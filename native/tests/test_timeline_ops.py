import unittest
from palmier.model import Editor, end


class TimelineOperationTests(unittest.TestCase):
    def setUp(self):
        self.e = Editor()
        self.e.new(fps=30, width=320, height=180)
        self.e.import_entry(dict(id="a", name="video", type="video", duration=10, hasAudio=True,
                                  source={"external": {"absolutePath": "/tmp/video.mp4"}}))
        self.e.import_entry(dict(id="b", name="audio", type="audio", duration=10, hasAudio=True,
                                  source={"external": {"absolutePath": "/tmp/audio.wav"}}))
        self.e.import_entry(dict(id="still", name="still", type="image", duration=5, hasAudio=False,
                                  source={"external": {"absolutePath": "/tmp/still.png"}}))
        self.e.add_track("video")

    def add(self, start=0, finish=90, track=0, media="a"):
        return self.e.add_clips([dict(mediaRef=media, trackIndex=track, startFrame=start, endFrame=finish)])["clipIds"][0]

    def spans(self, track=0):
        clips = sorted(self.e.timeline["tracks"][track]["clips"], key=lambda c: c["startFrame"])
        return [(c["startFrame"], end(c), c.get("trimStartFrame", 0)) for c in clips]

    def test_gaps_reject_unknown_track_before_indexing(self):
        self.assertEqual(self.e.gaps(0), [])
        with self.assertRaises(ValueError):
            self.e.gaps(4)
        self.add(30, 60)
        self.assertEqual(self.e.gaps(0), [[0, 30]])

    def test_insert_splits_spanning_clip_and_uses_source_range(self):
        self.add(0, 90)
        with self.assertRaisesRegex(ValueError, "both or neither"):
            self.e.insert_clips(0, [dict(mediaRef="a", startFrame=10)])
        with self.assertRaisesRegex(ValueError, "available source"):
            self.e.insert_clips(0, [dict(mediaRef="a", startFrame=0, endFrame=5000)])
        self.e.insert_clips(45, [dict(mediaRef="a", trackIndex=0, source=[1, 1.5])])
        self.assertEqual(self.spans(), [(0, 45, 0), (45, 60, 30), (60, 105, 45)])

    def test_insert_respects_sync_lock(self):
        self.add(0, 30)
        self.e.add_track("video")
        other = self.add(0, 20, track=1)
        self.e.timeline["tracks"][1]["syncLocked"] = False
        self.e.insert_clips(0, [dict(mediaRef="a", trackIndex=0, startFrame=0, endFrame=10)])
        self.assertEqual(self.e.clip(other)[1]["startFrame"], 0)
        self.e.timeline["tracks"][1]["syncLocked"] = True
        self.e.insert_clips(0, [dict(mediaRef="a", trackIndex=0, startFrame=0, endFrame=10)])
        self.assertEqual(self.e.clip(other)[1]["startFrame"], 10)

    def test_ripple_delete_applies_every_range_once(self):
        self.add(0, 90)
        self.add(120, 180)
        result = self.e.ripple_delete_ranges([
            dict(trackIndex=0, startFrame=30, endFrame=60),
            dict(trackIndex=0, startFrame=90, endFrame=120),
        ])
        self.assertEqual(self.spans(), [(0, 30, 0), (30, 60, 60), (60, 120, 0)])
        self.assertEqual(result["closedFrames"], 60)
        self.assertFalse(any(str(item).endswith("-split") for item in result["removed"]))

    def test_clear_effects_and_keyframe_types(self):
        cid = self.add()
        self.e.apply_effect([cid], [dict(type="blur", sigma=2)])
        self.e.apply_effect([cid], [])
        self.assertEqual(self.e.clip(cid)[1]["effects"], [])
        before = self.e.snapshot()
        with self.assertRaises(ValueError):
            self.e.set_keyframes(cid, dict(opacity=[dict(frame=0, value="nope")]))
        self.assertEqual(self.e.snapshot(), before)

    def test_copy_settings_reports_changed_clips_only(self):
        video = self.add(0, 30)
        image = self.add(40, 70, media="still")
        other = self.add(80, 100)
        self.e.properties([video], dict(opacity=0.25))
        result = self.e.copy_clip_settings(video, [image, other])
        self.assertEqual(result["clipIds"], [other])
        self.assertEqual(self.e.clip(other)[1]["opacity"], 0.25)
        self.assertEqual(self.e.clip(image)[1]["opacity"], 1)

    def test_linked_move_keeps_offset_and_trim_can_opt_out(self):
        video = self.add(0, 30)
        self.e.add_track("audio")
        audio = self.add(10, 40, track=1, media="b")
        self.e.manage_clip_links(link=[video, audio])
        self.e.move([dict(clipId=video, toFrame=20)])
        self.assertEqual(self.e.clip(audio)[1]["startFrame"], 30)
        self.e.move([dict(clipId=video, toFrame=5)], follow_links=False)
        self.assertEqual(self.e.clip(video)[1]["startFrame"], 5)
        self.assertEqual(self.e.clip(audio)[1]["startFrame"], 30)
        right = self.e.split([dict(clipId=video, atFrame=32)])["newClipIds"]
        self.assertGreaterEqual(len(right), 2)

    def test_sync_refuses_same_track_overlap(self):
        first = self.add(0, 30)
        second = self.add(40, 70)
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.e.sync_clips([first, second], to_frame=0)
        self.assertEqual(self.e.clip(second)[1]["startFrame"], 40)

    def test_duplicate_and_relink(self):
        cid = self.add(0, 30)
        dup = self.e.duplicate_clips([cid])["clipIds"][0]
        self.assertEqual(self.e.clip(dup)[1]["startFrame"], 30)
        self.e.relink_media("a", dict(name="relinked.mp4", type="video", duration=4, hasAudio=False,
                                      sourceWidth=320, sourceHeight=180, sourceFPS=30,
                                      source={"external": {"absolutePath": "/tmp/relinked.mp4"}}))
        self.assertEqual(self.e.media("a")["name"], "relinked.mp4")
        self.assertEqual(self.e.media("a")["duration"], 4)
        self.assertEqual(self.e.clip(cid)[1]["mediaRef"], "a")

    def test_denoise_requires_audio(self):
        cid = self.add()
        self.e.manifest["entries"][0]["hasAudio"] = False
        with self.assertRaisesRegex(ValueError, "audio"):
            self.e.set_denoise([cid], True, 0.4)
        self.e.manifest["entries"][0]["hasAudio"] = True
        self.e.set_denoise([cid], True, 0.4)
        self.assertEqual(self.e.clip(cid)[1]["denoise"]["strength"], 0.4)


if __name__ == "__main__":
    unittest.main()
