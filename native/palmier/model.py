import copy
import json
import math
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path


def uid():
    return str(uuid.uuid4()).upper()


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum or value > 100_000_000:
        raise ValueError(f"{name} must be an integer in [{minimum}, 100000000]")
    return value


def number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return value


def new_timeline(name="Timeline 1", fps=30, width=1920, height=1080):
    return dict(id=uid(), name=name, fps=fps, width=width, height=height,
                settingsConfigured=True, tracks=[], markers=[])


def new_track(kind, name=None):
    return dict(id=uid(), type=kind, name=name, muted=False, hidden=False,
                syncLocked=True, displayHeight=44, clips=[])


def end(clip):
    return clip["startFrame"] + clip["durationFrames"]


def merge_spans(spans):
    merged = []
    for start, finish in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([start, finish])
    return [(start, finish) for start, finish in merged]


def shift_before(frame, spans):
    total = 0
    for start, finish in merge_spans(spans):
        if start >= frame:
            break
        total += min(finish, frame) - start
    return total


def clip_defaults(kind, ref, start, duration, trim=0):
    return dict(id=uid(), mediaRef=ref, mediaType=kind, sourceClipType=kind,
                startFrame=start, durationFrames=duration, trimStartFrame=trim,
                trimEndFrame=0, speed=1.0, volume=1.0, opacity=1.0,
                fadeInFrames=0, fadeOutFrames=0, fadeInInterpolation="linear",
                fadeOutInterpolation="linear", edgeRounding=0.0, edgeSoftness=0.0,
                linkGroupId=None, color={}, effects=[],
                keyframes={}, denoise=None,
                transform=dict(centerX=.5, centerY=.5, width=1.0, height=1.0,
                               rotation=0.0, rotationX=0.0, rotationY=0.0,
                               flipHorizontal=False, flipVertical=False),
                crop=dict(left=0.0, top=0.0, right=0.0, bottom=0.0))


def validate_project(doc, manifest):
    if not isinstance(doc, dict) or not doc.get("timelines"):
        raise ValueError("Project must contain at least one timeline")
    ids = set()
    for t in doc["timelines"]:
        integer(t["fps"], "fps", 1)
        if t["fps"] > 120:
            raise ValueError("Frame rates above 120 are not supported")
        for key in ("width", "height"):
            integer(t[key], key, 2)
            if t[key] > 7680 or t[key] % 2:
                raise ValueError("Canvas must have even dimensions up to 7680")
        for m in t.get("markers", []):
            integer(m.get("startFrame", 0), "marker startFrame")
            integer(m.get("durationFrames", 0), "marker durationFrames")
            if not isinstance(m.get("name", ""), str) or len(m.get("name", "")) > 120:
                raise ValueError("Marker name must be a string up to 120 characters")
        for entity in [t, *t.get("tracks", []), *[c for tr in t.get("tracks", []) for c in tr["clips"]]]:
            if entity["id"] in ids:
                raise ValueError("Duplicate entity ID")
            ids.add(entity["id"])
        for tr in t.get("tracks", []):
            for c in tr["clips"]:
                integer(c["startFrame"], "startFrame")
                integer(c["durationFrames"], "durationFrames", 1)
                integer(c.get("trimStartFrame", 0), "trimStartFrame")
                integer(c.get("trimEndFrame", 0), "trimEndFrame")
                number(c.get("speed", 1), "speed", .05, 16)
                number(c.get("volume", 1), "volume", 0, 6)
                number(c.get("opacity", 1), "opacity", 0, 1)
                number(c.get("edgeRounding", 0), "edgeRounding", 0, 1)
                number(c.get("edgeSoftness", 0), "edgeSoftness", 0, 1)
                trf = c.get("transform", {})
                if trf:
                    for k in ("centerX", "centerY", "width", "height", "rotation", "rotationX", "rotationY"):
                        number(trf.get(k, 0), "transform." + k, -3600, 3600)
                cr = c.get("crop", {})
                if cr:
                    for k in ("left", "top", "right", "bottom"):
                        number(cr.get(k, 0), "crop." + k, 0, 1)
                    if sum(cr.get(k, 0) for k in ("left", "right")) >= 1 or sum(cr.get(k, 0) for k in ("top", "bottom")) >= 1:
                        raise ValueError("Crop insets must leave visible source")
                for fx in c.get("effects", []) or []:
                    if not isinstance(fx.get("type"), str):
                        raise ValueError("Effect type must be a string")
                for knob in ("exposure", "contrast", "saturation", "temperature", "highlights", "shadows", "sharpen"):
                    if knob in (c.get("color") or {}):
                        number(c["color"][knob], "color." + knob, -10, 10)
    if doc.get("activeTimelineId") not in {t["id"] for t in doc["timelines"]}:
        raise ValueError("activeTimelineId does not identify a timeline")
    if not isinstance(manifest.get("entries"), list):
        raise ValueError("media.json must contain entries")


class Editor:
    """One state owner and one transactional undo path for UI and MCP."""
    def __init__(self):
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.changed = lambda: None
        self.package = None
        self.name = "Untitled"
        self.doc = {}
        self.manifest = {}
        self.undo_stack = []
        self.redo_stack = []
        self.revision = 0
        self.dirty = False
        self.current_frame = 0
        self.mutation_depth = 0
        self.new()

    @property
    def timeline(self):
        return next(t for t in self.doc["timelines"] if t["id"] == self.doc["activeTimelineId"])

    def snapshot(self):
        with self.lock:
            return copy.deepcopy((self.doc, self.manifest))

    def new(self, name="Untitled", fps=30, width=1920, height=1080):
        with self.lock:
            t = new_timeline(fps=fps, width=width, height=height)
            doc = dict(timelines=[t], activeTimelineId=t["id"], openTimelineIds=[t["id"]])
            manifest = dict(version=2, entries=[], folders=[])
            validate_project(doc, manifest)
            self.doc, self.manifest = doc, manifest
            self.name, self.package = name, None
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.current_frame = 0
            self.dirty = False
            self.revision += 1
        self.changed()

    def mutate(self, label, action):
        with self.lock:
            before = self.snapshot()
            nested = self.mutation_depth > 0
            self.mutation_depth += 1
            try:
                result = action()
                validate_project(self.doc, self.manifest)
            except Exception:
                self.doc, self.manifest = before
                raise
            finally:
                self.mutation_depth -= 1
            if nested:
                return result
            if before != self.snapshot():
                self.undo_stack.append((label, before))
                self.undo_stack = self.undo_stack[-100:]
                self.redo_stack.clear()
                self.dirty = True
                self.revision += 1
            else:
                return result
        self.changed()
        return result

    def undo(self, redo=False):
        with self.lock:
            source, dest = (self.redo_stack, self.undo_stack) if redo else (self.undo_stack, self.redo_stack)
            if not source:
                raise ValueError("Nothing to redo" if redo else "Nothing to undo")
            label, snapshot = source.pop()
            dest.append((label, self.snapshot()))
            self.doc, self.manifest = snapshot
            self.dirty = True
            self.revision += 1
        self.changed()
        return dict(action=label)

    def load(self, package, expected_revision=None):
        with self.io_lock:
            return self._load(package, expected_revision)

    def _load(self, package, expected_revision):
        package = Path(package).resolve()
        doc = json.loads((package / "project.json").read_text(encoding="utf-8-sig"))
        if "timelines" not in doc and "tracks" in doc:
            doc = dict(timelines=[doc], activeTimelineId=doc["id"])
        if not doc.get("activeTimelineId") and doc.get("timelines"):
            doc["activeTimelineId"] = doc["timelines"][0]["id"]
        manifest_file = package / "media.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig")) if manifest_file.exists() else dict(version=2, entries=[], folders=[])
        validate_project(doc, manifest)
        with self.lock:
            if expected_revision is not None and self.revision != expected_revision:
                raise ValueError("Open cancelled: active project changed during file work")
            self.doc, self.manifest = doc, manifest
            self.package, self.name = package, package.stem
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.dirty = False
            self.current_frame = 0
            self.revision += 1
        self.changed()

    def save(self, destination=None):
        with self.io_lock:
            with self.lock:
                old_package = self.package
                doc, manifest = self.snapshot()
                revision = self.revision
                document_id = doc["timelines"][0]["id"]
            target = Path(destination or old_package or "").resolve()
            if not str(target).lower().endswith(".palmier"):
                raise ValueError("Choose a .palmier directory")
            if target.exists() and target != old_package:
                raise ValueError("Save As requires a new package; existing directories are never overwritten")
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".palmier-", dir=target.parent))
            backup = target.with_name("." + target.name + "." + uid() + ".backup")
            try:
                if target.exists():
                    shutil.copytree(target, stage, dirs_exist_ok=True)
                for entry in manifest["entries"]:
                    if "project" in entry.get("source", {}) and old_package != target:
                        if not old_package:
                            raise ValueError("Project-relative media has no original package")
                        relative = entry["source"]["project"]["relativePath"]
                        source = (old_package / relative).resolve()
                        if not source.is_relative_to(old_package.resolve()):
                            raise ValueError("Media path escapes project package")
                        if source.is_file():
                            relative = entry["source"]["project"]["relativePath"]
                            dest = (stage / relative).resolve()
                            if not dest.is_relative_to(stage.resolve()):
                                raise ValueError("Media path escapes project package")
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, dest)
                (stage / "media").mkdir(exist_ok=True)
                (stage / "project.json").write_text(json.dumps(doc, indent=2, allow_nan=False), encoding="utf-8")
                (stage / "media.json").write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
                with self.lock:
                    if self.doc["timelines"][0]["id"] != document_id:
                        raise ValueError("Save cancelled: active project changed during file work")
                    if target.exists():
                        os.replace(target, backup)
                    try:
                        os.replace(stage, target)
                    except Exception:
                        if backup.exists():
                            os.replace(backup, target)
                        raise
                    self.package, self.name = target, target.stem
                    self.dirty = self.revision != revision
                if backup.exists():
                    shutil.rmtree(backup)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        self.changed()
        return str(target)

    def media_path(self, entry):
        source = entry.get("source", {})
        if "external" in source:
            return Path(source["external"]["absolutePath"])
        if "project" in source and self.package:
            result = (self.package / source["project"]["relativePath"]).resolve()
            if not result.is_relative_to(self.package.resolve()):
                raise ValueError("Media path escapes project package")
            return result
        raise ValueError(f"Cannot resolve media {entry['id']}; relink the source")

    def media(self, ref):
        return next((m for m in self.manifest["entries"] if m["id"] == ref), None)

    def clip(self, clip_id):
        for tr in self.timeline["tracks"]:
            for c in tr["clips"]:
                if c["id"] == clip_id:
                    return tr, c
        raise ValueError("Unknown clip ID: " + clip_id)

    def add_track(self, kind):
        if kind not in ("video", "audio"):
            raise ValueError("Track must be video or audio")
        def action():
            track = new_track(kind)
            self.timeline["tracks"].append(track)
            return track["id"]
        return self.mutate("Add track", action)

    def import_entry(self, entry):
        return self.mutate("Import media", lambda: self.manifest["entries"].append(entry))

    @staticmethod
    def overwrite(track, start, finish, ignore=None):
        output = []
        for c in track["clips"]:
            if c["id"] == ignore or end(c) <= start or c["startFrame"] >= finish:
                output.append(c)
                continue
            original_end = end(c)
            if c["startFrame"] < start:
                left = copy.deepcopy(c)
                left["durationFrames"] = start - c["startFrame"]
                left["fadeOutFrames"] = 0
                output.append(left)
            if original_end > finish:
                right = copy.deepcopy(c)
                right["id"] = uid()
                right["trimStartFrame"] += round((finish - c["startFrame"]) * c.get("speed", 1))
                right["startFrame"], right["durationFrames"] = finish, original_end - finish
                right["fadeInFrames"] = 0
                output.append(right)
        track["clips"] = output

    def add_clips(self, entries):
        if not entries:
            raise ValueError("entries must not be empty")
        def action():
            result = []
            default_tracks = {}
            for e in entries:
                start = integer(e["startFrame"], "startFrame")
                media = self.media(e["mediaRef"])
                if not media:
                    raise ValueError("Unknown mediaRef")
                kind = media["type"]
                if kind not in ("video", "audio", "image"):
                    raise ValueError("Unsupported media type")
                trim = 0
                if "source" in e:
                    if "endFrame" in e or len(e["source"]) != 2:
                        raise ValueError("Use source [startSeconds,endSeconds] OR endFrame")
                    a, b = e["source"]
                    number(a, "source start", 0, media["duration"])
                    number(b, "source end", a, media["duration"])
                    trim, duration = round(a * self.timeline["fps"]), round((b-a) * self.timeline["fps"])
                else:
                    duration = e.get("endFrame", start + round(media["duration"] * self.timeline["fps"])) - start
                integer(duration, "durationFrames", 1)
                if kind != "image" and trim + duration > round(media["duration"] * self.timeline["fps"]):
                    raise ValueError("Clip exceeds available source duration")
                index = e.get("trackIndex")
                if index is None:
                    zone = "audio" if kind == "audio" else "video"
                    if zone not in default_tracks:
                        self.timeline["tracks"].append(new_track(zone))
                        default_tracks[zone] = len(self.timeline["tracks"]) - 1
                    index = default_tracks[zone]
                integer(index, "trackIndex")
                if index >= len(self.timeline["tracks"]):
                    raise ValueError("Unknown trackIndex")
                track = self.timeline["tracks"][index]
                if (track["type"] == "audio") != (kind == "audio"):
                    raise ValueError("Media is incompatible with destination track")
                c = clip_defaults(kind, media["id"], start, duration, trim)
                self.overwrite(track, start, start+duration)
                track["clips"].append(c)
                result.append(c["id"])
            return dict(clipIds=result)
        return self.mutate("Add clips", action)

    def move(self, moves, follow_links=True):
        if not moves:
            raise ValueError("moves must not be empty")
        def action():
            planned, seen = [], set()
            for m in moves:
                if m.get("clipId") in seen:
                    continue
                if "toFrame" not in m and "toTrack" not in m:
                    raise ValueError("Specify toFrame or toTrack")
                tr, c = self.clip(m["clipId"])
                index = m.get("toTrack", self.timeline["tracks"].index(tr))
                integer(index, "toTrack")
                if index >= len(self.timeline["tracks"]):
                    raise ValueError("Unknown track")
                dest = self.timeline["tracks"][index]
                if tr["type"] != dest["type"]:
                    raise ValueError("Cannot move across video/audio zones")
                frame = integer(m.get("toFrame", c["startFrame"]), "toFrame")
                planned.append((tr, c, dest, frame))
                seen.add(c["id"])
                if follow_links and c.get("linkGroupId") and "toFrame" in m:
                    delta = frame - c["startFrame"]
                    if delta:
                        for tr2 in self.timeline["tracks"]:
                            for other in tr2["clips"]:
                                if other["id"] in seen or other.get("linkGroupId") != c["linkGroupId"]:
                                    continue
                                partner = other["startFrame"] + delta
                                if partner < 0:
                                    raise ValueError("Linked clip would move before the start of the timeline")
                                planned.append((tr2, other, tr2, partner))
                                seen.add(other["id"])
            for tr, c, _, _ in planned:
                tr["clips"].remove(c)
            for _, c, dest, frame in planned:
                c["startFrame"] = frame
                self.overwrite(dest, frame, end(c))
                dest["clips"].append(c)
            return dict(clipIds=[c["id"] for _, c, _, _ in planned])
        return self.mutate("Move clips", action)

    def split(self, splits):
        def action():
            pending = []
            for s in splits:
                frame = integer(s["atFrame"], "atFrame")
                _, c = self.clip(s["clipId"])
                pending.append((c["id"], frame))
                group = c.get("linkGroupId")
                if not group:
                    continue
                for tr in self.timeline["tracks"]:
                    for other in tr["clips"]:
                        if other.get("linkGroupId") == group and other["id"] != c["id"] and other["startFrame"] < frame < end(other):
                            pending.append((other["id"], frame))
            result, seen = [], set()
            for cid, frame in pending:
                if (cid, frame) in seen:
                    continue
                seen.add((cid, frame))
                tr, c = self.clip(cid)
                if not c["startFrame"] < frame < end(c):
                    raise ValueError("Split point must be strictly inside the clip")
                if any(c.get(k) for k in ("positionTrack", "opacityTrack", "volumeTrack", "scaleTrack", "cropTrack", "multicamGroupId")):
                    raise ValueError("Splitting animated/multicam original clips is not implemented")
                right = self._split_clip(tr, c, frame)
                result.append(right["id"])
            return dict(newClipIds=result)
        return self.mutate("Split clips", action)

    def _split_clip(self, track, clip, frame):
        if not clip["startFrame"] < frame < end(clip):
            return None
        right = copy.deepcopy(clip)
        right["id"] = uid()
        offset = frame - clip["startFrame"]
        right["startFrame"], right["durationFrames"] = frame, clip["durationFrames"] - offset
        right["trimStartFrame"] += round(offset * clip.get("speed", 1))
        right["fadeInFrames"] = 0
        clip["durationFrames"], clip["fadeOutFrames"] = offset, 0
        track["clips"].append(right)
        return right

    def remove(self, clip_ids):
        def action():
            for cid in clip_ids:
                tr, c = self.clip(cid)
                tr["clips"].remove(c)
            return dict(removed=clip_ids)
        return self.mutate("Remove clips", action)

    def properties(self, ids, values):
        allowed = {"durationFrames", "trimStartFrame", "trimEndFrame", "speed", "volumeDb", "opacity",
                   "fadeInFrames", "fadeOutFrames", "edgeRounding", "edgeSoftness", "transform", "crop", "blendMode"}
        if not ids or not values or set(values) - allowed:
            raise ValueError("Unsupported or missing clip properties")
        def action():
            for cid in ids:
                _, c = self.clip(cid)
                for k, v in values.items():
                    if k == "volumeDb":
                        c["volume"] = 10 ** (number(v, k, -60, 15) / 20)
                    elif k == "opacity":
                        c[k] = number(v, k, 0, 1)
                    elif k == "speed":
                        number(v, k, .05, 16)
                        if "durationFrames" not in values:
                            c["durationFrames"] = max(1, round(c["durationFrames"] * c.get("speed", 1) / v))
                        c[k] = v
                    elif k == "transform":
                        if not isinstance(v, dict):
                            raise ValueError("transform must be an object")
                        for tk, tv in v.items():
                            if tk in ("flipHorizontal", "flipVertical"):
                                if not isinstance(tv, bool):
                                    raise ValueError("transform flip must be boolean")
                                c["transform"][tk] = tv
                            elif tk in ("centerX", "centerY", "width", "height", "rotation", "rotationX", "rotationY"):
                                c["transform"][tk] = number(tv, "transform." + tk, -3600, 3600)
                            else:
                                raise ValueError("Unknown transform field: " + tk)
                    elif k == "crop":
                        if not isinstance(v, dict):
                            raise ValueError("crop must be an object")
                        for ck, cv in v.items():
                            if ck not in ("left", "top", "right", "bottom"):
                                raise ValueError("Unknown crop field: " + ck)
                            c["crop"][ck] = number(cv, "crop." + ck, 0, 1)
                    elif k in ("edgeRounding", "edgeSoftness"):
                        c[k] = number(v, k, 0, 1)
                    elif k == "blendMode":
                        if v not in ("normal",):
                            raise ValueError("Only normal blend mode is rendered")
                        c[k] = v
                    else:
                        c[k] = integer(v, k, 1 if k == "durationFrames" else 0)
                if c.get("fadeInFrames", 0) + c.get("fadeOutFrames", 0) > c["durationFrames"]:
                    raise ValueError("Fades exceed clip duration")
                m = self.media(c["mediaRef"])
                if m and m["type"] not in ("image", "text") and c["trimStartFrame"] + round(c["durationFrames"] * c["speed"]) + c.get("trimEndFrame", 0) > round(m["duration"] * self.timeline["fps"]):
                    raise ValueError("Clip exceeds available media; shorten duration with trim")
            return dict(clipIds=ids)
        return self.mutate("Clip properties", action)

    def add_texts(self, entries):
        def action():
            ids = []
            for e in entries:
                if set(e) - {"startFrame", "endFrame", "content", "trackIndex", "style"}:
                    raise ValueError("Text animation/transform is not implemented")
                start = integer(e["startFrame"], "startFrame")
                duration = integer(e["endFrame"]-start, "durationFrames", 1)
                content = e["content"]
                if not isinstance(content, str) or len(content) > 10000:
                    raise ValueError("Text must be a string up to 10000 characters")
                style = e.get("style", {})
                if set(style) - {"fontSize", "color"}:
                    raise ValueError("Only fontSize and color text style are supported")
                size = number(style.get("fontSize", 96), "fontSize", 12, 300)
                color = style.get("color", "#FFFFFF")
                import re
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError("Text color must be #RRGGBB")
                index = e.get("trackIndex")
                if index is None:
                    self.timeline["tracks"].insert(0, new_track("video", "Text"))
                    index = 0
                if type(index) is not int or not 0 <= index < len(self.timeline["tracks"]) or self.timeline["tracks"][index]["type"] == "audio":
                    raise ValueError("Text requires a visual track")
                c = clip_defaults("text", uid(), start, duration)
                c["textContent"], c["textStyle"] = content, dict(fontSize=size, color=color)
                self.overwrite(self.timeline["tracks"][index], start, start+duration)
                self.timeline["tracks"][index]["clips"].append(c)
                ids.append(c["id"])
            return dict(clipIds=ids)
        return self.mutate("Add text", action)

    # ---- Linux fork extensions (ported from palmier-io concepts) ----

    def manage_markers(self, create=None, update=None, remove=None):
        def action():
            t = self.timeline
            t.setdefault("markers", [])
            created, updated = [], []
            for m in create or []:
                start = integer(m.get("startFrame", 0), "marker startFrame")
                dur = integer(m.get("durationFrames", 0), "marker durationFrames")
                name = m.get("name", "Marker")
                if not isinstance(name, str) or not name or len(name) > 120:
                    raise ValueError("Marker name must be 1..120 characters")
                color = m.get("color", "#E8B44F")
                import re
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError("Marker color must be #RRGGBB")
                entry = dict(markerId=uid(), name=name, comment=m.get("comment", ""),
                             color=color, startFrame=start, endFrame=start + dur, durationFrames=dur)
                t["markers"].append(entry)
                created.append(entry["markerId"])
            for m in update or []:
                entry = next((x for x in t["markers"] if x["markerId"] == m["markerId"]), None)
                if entry is None:
                    raise ValueError("Unknown markerId: " + m["markerId"])
                if "name" in m:
                    if not isinstance(m["name"], str) or len(m["name"]) > 120:
                        raise ValueError("Marker name must be up to 120 characters")
                    entry["name"] = m["name"]
                if "comment" in m:
                    entry["comment"] = m["comment"]
                if "startFrame" in m:
                    entry["startFrame"] = integer(m["startFrame"], "marker startFrame")
                    entry["endFrame"] = entry["startFrame"] + entry["durationFrames"]
                if "durationFrames" in m:
                    entry["durationFrames"] = integer(m["durationFrames"], "marker durationFrames")
                    entry["endFrame"] = entry["startFrame"] + entry["durationFrames"]
                updated.append(entry["markerId"])
            for mid in remove or []:
                entry = next((x for x in t["markers"] if x["markerId"] == mid), None)
                if entry is None:
                    raise ValueError("Unknown markerId: " + mid)
                t["markers"].remove(entry)
            return dict(created=created, updated=updated, removed=remove or [],
                        markers=copy.deepcopy(t["markers"]))
        return self.mutate("Manage markers", action)

    def _entry_span(self, media, entry):
        if not isinstance(entry, dict):
            raise ValueError("Clip entry must be an object")
        kind = media["type"]
        fps = self.timeline["fps"]
        full = round(media["duration"] * fps)
        if "source" in entry:
            if "startFrame" in entry or "endFrame" in entry:
                raise ValueError("Use source [startSeconds, endSeconds] OR startFrame/endFrame")
            if len(entry["source"]) != 2:
                raise ValueError("source must be [startSeconds, endSeconds]")
            a, b = entry["source"]
            number(a, "source start", 0, media["duration"])
            number(b, "source end", a, media["duration"])
            trim, dur = round(a * fps), round((b - a) * fps)
        else:
            has_start, has_end = "startFrame" in entry, "endFrame" in entry
            if has_start != has_end:
                raise ValueError("startFrame and endFrame together set the inserted length; pass both or neither")
            if has_start:
                start = integer(entry["startFrame"], "startFrame")
                finish = integer(entry["endFrame"], "endFrame")
                if finish <= start:
                    raise ValueError("endFrame must be after startFrame")
                trim, dur = 0, finish - start
            else:
                trim, dur = 0, full
        if dur < 1:
            raise ValueError("Clip duration must be at least 1 frame")
        if kind != "image" and trim + dur > full:
            raise ValueError("Clip exceeds available source duration")
        return trim, dur

    def insert_clips(self, at_frame, entries):
        at_frame = integer(at_frame, "atFrame")
        if not entries:
            raise ValueError("entries must not be empty")
        def action():
            if not self.timeline["tracks"]:
                self.timeline["tracks"].append(new_track("video"))
            staged = []
            for e in entries:
                media = self.media(e.get("mediaRef") if isinstance(e, dict) else None)
                if not media:
                    raise ValueError("Unknown mediaRef")
                kind = media["type"]
                if kind not in ("video", "audio", "image"):
                    raise ValueError("Unsupported media type")
                trim, dur = self._entry_span(media, e)
                index = integer(e.get("trackIndex", 0), "trackIndex")
                if index >= len(self.timeline["tracks"]):
                    raise ValueError("Unknown trackIndex")
                track = self.timeline["tracks"][index]
                if (track["type"] == "audio") != (kind == "audio"):
                    raise ValueError("Media is incompatible with destination track")
                staged.append((track, media, kind, trim, dur))
            total_push = sum(item[4] for item in staged)
            dest_ids = {item[0]["id"] for item in staged}
            dest_sync = any(track.get("syncLocked", True) for track in self.timeline["tracks"] if track["id"] in dest_ids)

            def ripples(track):
                if track["id"] in dest_ids:
                    return True
                return bool(dest_sync and track.get("syncLocked", True))

            for track in self.timeline["tracks"]:
                if not ripples(track):
                    continue
                for clip in list(track["clips"]):
                    self._split_clip(track, clip, at_frame)
                for clip in track["clips"]:
                    if clip["startFrame"] >= at_frame:
                        clip["startFrame"] += total_push
            result, cursor = [], at_frame
            for track, media, kind, trim, dur in staged:
                clip = clip_defaults(kind, media["id"], cursor, dur, trim)
                track["clips"].append(clip)
                result.append(clip["id"])
                cursor += dur
            return dict(clipIds=result, pushedFrames=total_push)
        return self.mutate("Insert clips (ripple)", action)

    def _subtract_clip(self, clip, spans, removed):
        pieces = [clip]
        for start, finish in merge_spans(spans):
            nxt = []
            for c in pieces:
                cs, ce = c["startFrame"], end(c)
                if ce <= start or cs >= finish:
                    nxt.append(c)
                    continue
                speed = c.get("speed", 1)
                trim = c.get("trimStartFrame", 0)
                if cs < start and ce > finish:
                    right = copy.deepcopy(c)
                    right["id"] = uid()
                    right["trimStartFrame"] = trim + round((finish - cs) * speed)
                    right["startFrame"] = finish
                    right["durationFrames"] = ce - finish
                    right["fadeInFrames"] = 0
                    c["durationFrames"] = start - cs
                    c["fadeOutFrames"] = 0
                    nxt.extend([c, right])
                elif cs < start:
                    c["durationFrames"] = start - cs
                    c["fadeOutFrames"] = 0
                    nxt.append(c)
                elif ce > finish:
                    c["trimStartFrame"] = trim + round((finish - cs) * speed)
                    c["startFrame"] = finish
                    c["durationFrames"] = ce - finish
                    c["fadeInFrames"] = 0
                    nxt.append(c)
                else:
                    removed.append(c["id"])
            pieces = nxt
        return pieces

    def ripple_delete_ranges(self, ranges):
        if not ranges:
            raise ValueError("ranges must not be empty")
        def action():
            per_track = {}
            for r in ranges:
                if not isinstance(r, dict):
                    raise ValueError("Range must be an object")
                index = integer(r.get("trackIndex", 0), "trackIndex")
                start = integer(r.get("startFrame", 0), "startFrame")
                finish = integer(r["endFrame"], "endFrame") if "endFrame" in r else integer(r.get("endFrame", start + 1), "endFrame")
                if finish <= start:
                    raise ValueError("endFrame must be after startFrame")
                if index >= len(self.timeline["tracks"]):
                    raise ValueError("Unknown trackIndex")
                per_track.setdefault(index, []).append((start, finish))
                if self.timeline["tracks"][index].get("syncLocked", True):
                    for other, track in enumerate(self.timeline["tracks"]):
                        if other != index and track.get("syncLocked", True):
                            per_track.setdefault(other, []).append((start, finish))
            removed = []
            for index, spans in per_track.items():
                track = self.timeline["tracks"][index]
                pieces = []
                for clip in list(track["clips"]):
                    pieces.extend(self._subtract_clip(clip, spans, removed))
                for clip in pieces:
                    clip["startFrame"] -= shift_before(clip["startFrame"], spans)
                track["clips"] = sorted(pieces, key=lambda c: (c["startFrame"], c["id"]))
            closed = max((sum(finish - start for start, finish in merge_spans(spans)) for spans in per_track.values()), default=0)
            return dict(removed=removed, closedFrames=closed)
        return self.mutate("Ripple delete", action)

    def swap_clip_media(self, swaps):
        if not swaps:
            raise ValueError("swaps must not be empty")
        def action():
            for s in swaps:
                _, c = self.clip(s["clipId"])
                media = self.media(s["mediaRef"])
                if not media:
                    raise ValueError("Unknown mediaRef")
                if media["type"] != c["mediaType"] and not (media["type"] in ("video", "image") and c["mediaType"] in ("video", "image")):
                    raise ValueError("Replacement media type is incompatible")
                c["mediaRef"] = media["id"]
            return dict(swapped=[s["clipId"] for s in swaps])
        return self.mutate("Swap clip media", action)

    def copy_clip_settings(self, source_id, target_ids=None, target_track=None, start_frame=None, end_frame=None):
        _, src = self.clip(source_id)
        portable = {k: copy.deepcopy(src.get(k)) for k in ("transform", "crop", "opacity", "color", "effects", "textStyle", "volume", "edgeRounding", "edgeSoftness") if src.get(k) is not None}
        def action():
            if target_ids is not None:
                targets = [self.clip(cid)[1] for cid in target_ids]
            elif target_track is not None:
                track = next((t for t in self.timeline["tracks"] if t["id"] == target_track), None)
                if track is None:
                    raise ValueError("Unknown target track")
                targets = [c for c in track["clips"] if c["mediaType"] == src["mediaType"] and c["id"] != source_id
                           and (start_frame is None or end(c) > start_frame) and (end_frame is None or c["startFrame"] < end_frame)]
                if not targets:
                    raise ValueError("No compatible clips match the target selector")
            else:
                raise ValueError("Specify targetClipIds or targetTrack")
            changed_ids = []
            for c in targets:
                if c["id"] == source_id or c["mediaType"] != src["mediaType"]:
                    continue
                for k, v in portable.items():
                    c[k] = copy.deepcopy(v)
                changed_ids.append(c["id"])
            if not changed_ids:
                raise ValueError("No compatible clips match the target selector")
            return dict(changed=len(changed_ids), clipIds=changed_ids)
        return self.mutate("Copy clip settings", action)

    def manage_clip_links(self, link=None, unlink=None):
        def action():
            if link:
                group = uid()
                for cid in link:
                    _, c = self.clip(cid)
                    c["linkGroupId"] = group
                return dict(linkGroupId=group, clipIds=link)
            result = []
            for cid in unlink or []:
                _, c = self.clip(cid)
                c["linkGroupId"] = None
                result.append(cid)
            return dict(unlinked=result)
        return self.mutate("Manage clip links", action)

    def sync_clips(self, clip_ids, to_frame=None):
        if not clip_ids:
            raise ValueError("clipIds must not be empty")
        def action():
            chosen = []
            for cid in clip_ids:
                chosen.append(self.clip(cid))
            target = chosen[0][1]["startFrame"] if to_frame is None else integer(to_frame, "toFrame")
            for _, c in chosen:
                c["startFrame"] = target
            for tr, c in chosen:
                for other in tr["clips"]:
                    if other is c:
                        continue
                    if other["startFrame"] < end(c) and end(other) > c["startFrame"]:
                        raise ValueError("Sync would overlap clips on the same track")
            return dict(clipIds=list(clip_ids), toFrame=target)
        return self.mutate("Sync clips", action)

    LAYOUTS = {
        "full": {"main": (0.5, 0.5, 1.0, 1.0)},
        "side_by_side": {"left": (0.25, 0.5, 0.5, 1.0), "right": (0.75, 0.5, 0.5, 1.0)},
        "top_bottom": {"top": (0.5, 0.25, 1.0, 0.5), "bottom": (0.5, 0.75, 1.0, 0.5)},
        "pip_bottom_right": {"main": (0.5, 0.5, 1.0, 1.0), "inset": (0.78, 0.72, 0.32, 0.32)},
        "pip_bottom_left": {"main": (0.5, 0.5, 1.0, 1.0), "inset": (0.22, 0.72, 0.32, 0.32)},
        "pip_top_right": {"main": (0.5, 0.5, 1.0, 1.0), "inset": (0.78, 0.28, 0.32, 0.32)},
        "pip_top_left": {"main": (0.5, 0.5, 1.0, 1.0), "inset": (0.22, 0.28, 0.32, 0.32)},
        "grid_2x2": {"r1c1": (0.25, 0.25, 0.5, 0.5), "r1c2": (0.75, 0.25, 0.5, 0.5),
                     "r2c1": (0.25, 0.75, 0.5, 0.5), "r2c2": (0.75, 0.75, 0.5, 0.5)},
        "main_sidebar": {"main": (0.35, 0.5, 0.7, 1.0), "sidebar": (0.85, 0.5, 0.3, 1.0)},
        "three_up": {"left": (1/6, 0.5, 1/3, 1.0), "center": (0.5, 0.5, 1/3, 1.0), "right": (5/6, 0.5, 1/3, 1.0)},
        "three_stack": {"top": (0.5, 1/6, 1.0, 1/3), "middle": (0.5, 0.5, 1.0, 1/3), "bottom": (0.5, 5/6, 1.0, 1/3)},
    }

    def apply_layout(self, layout, slots, fit="cover"):
        if layout not in self.LAYOUTS:
            raise ValueError("Unknown layout: " + layout)
        spec = self.LAYOUTS[layout]
        if set(slots) != set(spec):
            raise ValueError(f"Layout {layout} needs slots {sorted(spec)}")
        if fit not in ("cover", "fit"):
            raise ValueError("fit must be cover or fit")
        def action():
            touched = []
            for name, entry in slots.items():
                cx, cy, w, h = spec[name]
                for cid in entry.get("clipIds", []):
                    _, c = self.clip(cid)
                    if c["mediaType"] == "audio":
                        raise ValueError("Layouts apply to visual clips only")
                    c["transform"].update(dict(centerX=cx, centerY=cy, width=w, height=h, fit=fit))
                    if fit == "cover":
                        c["crop"] = dict(left=0.0, top=0.0, right=0.0, bottom=0.0)
                    touched.append(cid)
            return dict(layout=layout, clipIds=touched)
        return self.mutate("Apply layout", action)

    def add_captions(self, cues, track_index=None, style=None):
        if not cues:
            raise ValueError("cues must not be empty")
        def action():
            if track_index is None:
                self.timeline["tracks"].insert(0, new_track("video", "Captions"))
                index = 0
            else:
                index = integer(track_index, "trackIndex")
                if index >= len(self.timeline["tracks"]):
                    raise ValueError("Unknown trackIndex")
            group = uid()
            ids = []
            for cue in cues:
                start = integer(cue["startFrame"], "startFrame")
                finish = integer(cue["endFrame"], "endFrame")
                if finish <= start:
                    raise ValueError("Caption endFrame must be after startFrame")
                text = cue.get("text", "")
                if not isinstance(text, str) or not text or len(text) > 1000:
                    raise ValueError("Caption text must be 1..1000 characters")
                c = clip_defaults("text", uid(), start, finish - start)
                c["textContent"] = text
                import re
                size = number((style or {}).get("fontSize", 64), "fontSize", 12, 300)
                color = (style or {}).get("color", "#FFFFFF")
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError("Text color must be #RRGGBB")
                c["textStyle"] = dict(fontSize=size, color=color)
                c["captionGroupId"] = group
                self.overwrite(self.timeline["tracks"][index], start, finish)
                self.timeline["tracks"][index]["clips"].append(c)
                ids.append(c["id"])
            return dict(clipIds=ids, captionGroupId=group)
        return self.mutate("Add captions", action)

    def apply_color(self, clip_ids, grade=None, reset=False):
        if not clip_ids:
            raise ValueError("clipIds must not be empty")
        if grade is None and not reset:
            raise ValueError("Supply a grade object or reset:true")
        def action():
            for cid in clip_ids:
                _, c = self.clip(cid)
                if c["mediaType"] == "audio":
                    raise ValueError("Color grades apply to visual clips only")
                if reset:
                    c["color"] = {}
                else:
                    if not isinstance(grade, dict):
                        raise ValueError("grade must be an object")
                    for k, v in grade.items():
                        if k not in ("exposure", "contrast", "saturation", "temperature", "highlights", "shadows", "sharpen"):
                            raise ValueError("Unknown color knob: " + k)
                        number(v, "color." + k, -10, 10)
                    current = dict(c.get("color") or {})
                    current.update(grade)
                    c["color"] = current
            _, first = self.clip(clip_ids[0])
            return dict(clipIds=clip_ids, color=copy.deepcopy(first.get("color") or {}))
        return self.mutate("Apply color", action)

    def apply_effect(self, clip_ids, effects):
        if not clip_ids:
            raise ValueError("clipIds must not be empty")
        if not isinstance(effects, list):
            raise ValueError("effects must be a list")
        supported = {"grayscale", "blur", "sharpen", "vignette", "mirror", "denoise-light"}
        def action():
            for cid in clip_ids:
                _, c = self.clip(cid)
                if not effects:
                    c["effects"] = []
                    continue
                if c["mediaType"] == "audio":
                    raise ValueError("Effects apply to visual clips only")
                stack = list(c.get("effects") or [])
                for fx in effects:
                    if not isinstance(fx, dict):
                        raise ValueError("Effect must be an object")
                    ftype = fx.get("type")
                    if ftype not in supported:
                        raise ValueError("Unsupported effect: " + str(ftype))
                    params = {k: v for k, v in fx.items() if k != "type"}
                    stack = [e for e in stack if e.get("type") != ftype]
                    stack.append(dict(type=ftype, params=params))
                c["effects"] = stack
            return dict(clipIds=clip_ids, cleared=not effects)
        return self.mutate("Apply effect", action)

    def set_denoise(self, clip_ids, enabled=True, strength=0.6):
        if not clip_ids:
            raise ValueError("clipIds must not be empty")
        number(strength, "strength", 0, 1)
        def action():
            for cid in clip_ids:
                _, c = self.clip(cid)
                media = self.media(c["mediaRef"]) if c.get("mediaType") != "text" else None
                audible = c["mediaType"] == "audio" or bool(media and media.get("hasAudio"))
                if not audible:
                    raise ValueError("Denoise applies to clips with audio")
                c["denoise"] = None if not enabled else dict(strength=strength)
            return dict(clipIds=clip_ids, enabled=enabled)
        return self.mutate("Denoise audio", action)

    def set_keyframes(self, clip_id, tracks):
        if not tracks:
            raise ValueError("tracks must not be empty")
        allowed = {"opacity", "volume", "rotation", "scale", "position", "crop"}
        def action():
            _, c = self.clip(clip_id)
            store = dict(c.get("keyframes") or {})
            for name, keys in tracks.items():
                if name not in allowed:
                    raise ValueError("Unknown keyframe track: " + name)
                if not isinstance(keys, list) or not keys:
                    raise ValueError("Keyframe track must be a non-empty list")
                cleaned = []
                for k in keys:
                    if not isinstance(k, dict):
                        raise ValueError("Keyframe must be an object")
                    frame, value = k.get("frame"), k.get("value")
                    if type(frame) is not int:
                        raise ValueError("Keyframe frame must be an integer")
                    integer(frame, "keyframe frame")
                    if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError("Keyframe value must be a finite number")
                    cleaned.append(dict(k, frame=frame, value=value))
                store[name] = sorted(cleaned, key=lambda k: k["frame"])
            c["keyframes"] = store
            return dict(clipId=clip_id, tracks=sorted(store))
        return self.mutate("Set keyframes", action)

    def gaps(self, track_index):
        index = integer(track_index, "trackIndex")
        if index >= len(self.timeline["tracks"]):
            raise ValueError("Unknown trackIndex")
        track = self.timeline["tracks"][index]
        spans = sorted((c["startFrame"], end(c)) for c in track["clips"])
        out, cursor = [], 0
        for s, e in spans:
            if s > cursor:
                out.append([cursor, s])
            cursor = max(cursor, e)
        return out

    def duplicate_clips(self, clip_ids):
        if not clip_ids:
            raise ValueError("clipIds must not be empty")
        def action():
            created = []
            for cid in list(clip_ids):
                tr, c = self.clip(cid)
                dup = copy.deepcopy(c)
                dup["id"] = uid()
                dup["linkGroupId"] = None
                dup["startFrame"] = end(c)
                self.overwrite(tr, dup["startFrame"], end(dup))
                tr["clips"].append(dup)
                created.append(dup["id"])
            return dict(clipIds=created)
        return self.mutate("Duplicate clips", action)

    def relink_media(self, media_id, probed):
        if not isinstance(probed, dict):
            raise ValueError("Replacement media is invalid")
        def action():
            current = self.media(media_id)
            if current is None:
                raise ValueError("Unknown media id")
            kind = probed.get("type")
            if kind not in ("video", "audio", "image"):
                raise ValueError("Unsupported media type")
            if kind != current["type"] and not (kind in ("video", "image") and current["type"] in ("video", "image")):
                raise ValueError("Replacement media type is incompatible")
            source = probed.get("source")
            if not isinstance(source, dict) or ("external" not in source and "project" not in source):
                raise ValueError("Replacement media has no source")
            current["source"] = copy.deepcopy(source)
            current["name"] = probed.get("name", current["name"])
            current["duration"] = number(probed.get("duration", 0), "duration", 0.01, 86400 * 24)
            current["type"] = kind
            for key in ("sourceWidth", "sourceHeight", "sourceFPS", "hasAudio"):
                if key in probed:
                    current[key] = probed[key]
            return dict(mediaId=media_id, name=current["name"])
        return self.mutate("Relink media", action)
