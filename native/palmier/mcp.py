import copy
import hmac
import json
import math
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from jsonschema import Draft202012Validator
from . import VERSION
from .model import integer, new_timeline, uid, end
from .media import probe, duration

ORIGINAL_TOOLS = "manage_project get_timeline inspect_timeline create_timeline set_active_timeline manage_markers set_project_settings export_project manage_exports get_media inspect_media search_media import_media capture_frame organize_media manage_tracks manage_clip_links add_clips insert_clips move_clips remove_clips split_clips ripple_delete_ranges swap_clip_media set_clip_properties copy_clip_settings set_keyframes apply_layout sync_clips undo manage_multicam change_cam get_multicam get_transcript remove_words remove_silence detect_beats add_texts update_text add_captions apply_color apply_effect inspect_color denoise_audio list_models generate_video generate_image generate_audio upscale_media send_feedback".split()


def obj(properties=None, required=None):
    result = dict(type="object", properties=properties or {}, additionalProperties=False)
    if required:
        result["required"] = required
    return result


def array(schema):
    return dict(type="array", items=schema, minItems=1)


S = dict(type="string")
I = dict(type="integer", minimum=0, maximum=100_000_000)
N = dict(type="number")
B = dict(type="boolean")
STYLE = obj(dict(fontSize=dict(type="number", minimum=12, maximum=300), color=dict(type="string", pattern=r"^#[0-9a-fA-F]{6}$")))
GRADE = obj(dict(exposure=N, contrast=N, saturation=N, temperature=N, highlights=N, shadows=N, sharpen=N))
FX = obj(dict(type=dict(enum=["grayscale", "blur", "sharpen", "vignette", "mirror", "denoise-light"]),
              sigma=N, amount=N), ["type"])
SCHEMAS = {
    "manage_project": obj(dict(action=dict(enum=["list", "open", "create", "close"]), name=S, id=S, path=S, fps=I,
                               aspectRatio=dict(enum=["16:9", "9:16", "1:1", "4:3"]), quality=dict(enum=["720p", "1080p", "2K", "4K"])), ["action"]),
    "get_timeline": obj(dict(startFrame=I, endFrame=I, captionDetail=B)),
    "inspect_timeline": obj(dict(startFrame=I, endFrame=I, captionDetail=B)),
    "manage_markers": obj(dict(create=array(obj(dict(startFrame=I, durationFrames=I, name=S, comment=S, color=S))),
                                update=array(obj(dict(markerId=S, startFrame=I, durationFrames=I, name=S, comment=S))),
                                remove=array(S))),
    "get_media": obj(dict(ids=array(S), folder=S, pending=B)),
    "inspect_media": obj(dict(ids=array(S)), ["ids"]),
    "search_media": obj(dict(query=S, limit=I), ["query"]),
    "organize_media": obj(dict(createFolders=array(S), rename=array(obj(dict(folder=S, name=S), ["folder", "name"])),
                                move=array(obj(dict(ids=array(S), folder=S), ["ids"])))),
    "import_media": obj(dict(source=obj(dict(path=S), ["path"]), name=S), ["source"]),
    "capture_frame": obj(dict(frame=I, outputPath=S)),
    "add_clips": obj(dict(entries=array(obj(dict(mediaRef=S, trackIndex=I, startFrame=I, endFrame=I,
                                                 source=dict(type="array", items=N, minItems=2, maxItems=2)), ["mediaRef", "startFrame"]))), ["entries"]),
    "insert_clips": obj(dict(atFrame=I, entries=array(obj(dict(mediaRef=S, trackIndex=I, startFrame=I, endFrame=I,
                                                             source=dict(type="array", items=N, minItems=2, maxItems=2)), ["mediaRef"]))), ["atFrame", "entries"]),
    "move_clips": obj(dict(moves=array(obj(dict(clipId=S, toTrack=I, toFrame=I), ["clipId"]))), ["moves"]),
    "remove_clips": obj(dict(clipIds=array(S)), ["clipIds"]),
    "split_clips": obj(dict(splits=array(obj(dict(clipId=S, atFrame=I), ["clipId", "atFrame"]))), ["splits"]),
    "ripple_delete_ranges": obj(dict(ranges=array(obj(dict(trackIndex=I, startFrame=I, endFrame=I), ["startFrame", "endFrame"]))), ["ranges"]),
    "swap_clip_media": obj(dict(swaps=array(obj(dict(clipId=S, mediaRef=S), ["clipId", "mediaRef"]))), ["swaps"]),
    "copy_clip_settings": obj(dict(sourceClipId=S, targetClipIds=array(S), targetTrack=S, startFrame=I, endFrame=I), ["sourceClipId"]),
    "set_clip_properties": obj(dict(clipIds=array(S), durationFrames=I, trimStartFrame=I, trimEndFrame=I,
                                     speed=dict(type="number", minimum=.05, maximum=16),
                                     volumeDb=dict(type="number", minimum=-60, maximum=15),
                                     opacity=dict(type="number", minimum=0, maximum=1), fadeInFrames=I, fadeOutFrames=I,
                                     edgeRounding=N, edgeSoftness=N,
                                     transform=obj(dict(centerX=N, centerY=N, width=N, height=N, rotation=N, flipHorizontal=B, flipVertical=B)),
                                     crop=obj(dict(left=N, top=N, right=N, bottom=N))), ["clipIds"]),
    "set_keyframes": obj(dict(clipId=S, tracks=obj(dict(opacity=array(obj(dict(frame=I, value=N), ["frame", "value"])),
                                                              volume=array(obj(dict(frame=I, value=N), ["frame", "value"])),
                                                              rotation=array(obj(dict(frame=I, value=N), ["frame", "value"])),
                                                              scale=array(obj(dict(frame=I, value=N), ["frame", "value"])),
                                                              position=array(obj(dict(frame=I, value=N), ["frame", "value"])),
                                                              crop=array(obj(dict(frame=I, value=N), ["frame", "value"]))))), ["clipId", "tracks"]),
    "apply_layout": obj(dict(layout=dict(enum=["full", "side_by_side", "top_bottom", "pip_bottom_right", "pip_bottom_left",
                                                        "pip_top_right", "pip_top_left", "grid_2x2", "main_sidebar",
                                                        "three_up", "three_stack"]),
                              slots=dict(type="object", minProperties=1, additionalProperties=obj(dict(clipIds=array(S)), ["clipIds"])),
                              fit=dict(enum=["cover", "fit"])), ["layout", "slots"]),
    "sync_clips": obj(dict(clipIds=array(S), toFrame=I), ["clipIds"]),
    "manage_clip_links": obj(dict(link=array(S), unlink=array(S))),
    "manage_tracks": obj(dict(set=array(obj(dict(trackId=S, index=I, muted=B, hidden=B, syncLocked=B, name=dict(type="string", maxLength=80)))),
                               reorder=array(obj(dict(trackId=S, index=I, to=I), ["to"])),
                               remove=array(dict(oneOf=[I, obj(dict(trackId=S), ["trackId"])])))),
    "add_texts": obj(dict(entries=array(obj(dict(trackIndex=I, startFrame=I, endFrame=I, content=S, style=STYLE), ["startFrame", "endFrame", "content"]))), ["entries"]),
    "update_text": obj(dict(clipIds=array(S), content=S, style=STYLE), ["clipIds"]),
    "add_captions": obj(dict(cues=array(obj(dict(startFrame=I, endFrame=I, text=S), ["startFrame", "endFrame", "text"])),
                              trackIndex=I, style=STYLE), ["cues"]),
    "apply_color": obj(dict(clipIds=array(S), color=GRADE, reset=B), ["clipIds"]),
    "apply_effect": obj(dict(clipIds=array(S), effects=dict(type="array", items=FX)), ["clipIds", "effects"]),
    "inspect_color": obj(dict(clipId=S, mediaRef=S)),
    "denoise_audio": obj(dict(clipIds=array(S), enabled=B, strength=N), ["clipIds"]),
    "remove_silence": obj(dict(trackIndex=I, thresholdDb=N, minLength=N), []),
    "get_transcript": obj(dict(trackIndex=I, startFrame=I, endFrame=I, granularity=dict(enum=["words", "segments"]))),
    "detect_beats": obj(dict(trackIndex=I)),
    "manage_multicam": obj(dict(action=dict(enum=["create", "disband"]), clipIds=array(S), groupId=S), ["action"]),
    "change_cam": obj(dict(groupId=S, angle=I, startFrame=I, endFrame=I), ["groupId", "angle"]),
    "get_multicam": obj(dict(groupId=S)),
    "list_models": obj(),
    "generate_video": obj(dict(prompt=S, duration=N)),
    "generate_image": obj(dict(prompt=S)),
    "generate_audio": obj(dict(prompt=S)),
    "upscale_media": obj(dict(mediaRef=S)),
    "send_feedback": obj(dict(kind=S, message=S)),
    "undo": obj(),
    "create_timeline": obj(dict(name=S)),
    "set_active_timeline": obj(dict(timelineId=S), ["timelineId"]),
    "set_project_settings": obj(dict(width=I, height=I)),
    "export_project": obj(dict(mode=dict(enum=["video"]), codec=dict(enum=["H.264", "H.265"]),
                               resolution=dict(enum=["Match Timeline", "720p", "1080p", "2K", "4K"]),
                               outputPath=S, overwrite=B, timelineId=S, bitrateMbps=dict(type="number", minimum=.1, maximum=200), hardware=B)),
    "manage_exports": obj(dict(action=dict(enum=["list", "cancel"]), jobId=S), ["action"]),
    "windows_playback": obj(dict(action=dict(enum=["play", "pause", "seek"]), frame=I), ["action"]),
    "linux_playback": obj(dict(action=dict(enum=["play", "pause", "seek"]), frame=I), ["action"]),
    "windows_redo": obj(),
    "linux_redo": obj(),
}


class Tools:
    def __init__(self, editor, exports):
        self.editor = editor
        self.exports = exports
        self.playback = lambda action, frame: (_ for _ in ()).throw(ValueError("No active desktop playback controller"))

    def list(self):
        result = []
        for name, schema in SCHEMAS.items():
            if name.startswith(("windows_", "linux_")):
                prefix = "Linux extension. " if name.startswith("linux_") else "Windows extension (kept for compatibility). "
            else:
                prefix = "GPL Palmier tool name; supported Linux subset. "
            result.append(dict(name=name, description=prefix +
                               f"{name.replace('_',' ')}. Only properties in this schema are implemented; unsupported original modes fail without mutation.", inputSchema=schema))
        return result

    def timeline_payload(self, e, args):
        t = copy.deepcopy(e.timeline)
        start, finish = args.get("startFrame", 0), args.get("endFrame", 100_000_000)
        if finish < start:
            raise ValueError("endFrame precedes startFrame")
        tracks = []
        for index, tr in enumerate(t["tracks"]):
            clips = []
            for c in tr["clips"]:
                if end(c) > start and c["startFrame"] < finish:
                    m = e.media(c["mediaRef"])
                    clips.append(dict(id=c["id"], mediaRef=c["mediaRef"], type=c["mediaType"],
                                      frames=[c["startFrame"], end(c)], name=m["name"] if m else c.get("textContent", "Missing media"),
                                      speed=c.get("speed", 1), opacity=c.get("opacity", 1),
                                      volumeDb=20*math.log10(max(c.get("volume", 1), .001)),
                                      transform=c.get("transform"), crop=c.get("crop"),
                                      color=c.get("color"), effects=c.get("effects"),
                                      linkGroupId=c.get("linkGroupId"),
                                      text=c.get("textContent") if c["mediaType"] == "text" else None))
            tracks.append(dict(trackId=tr["id"], index=index, type=tr["type"], name=tr.get("name"),
                               muted=tr.get("muted", False), hidden=tr.get("hidden", False),
                               syncLocked=tr.get("syncLocked", True), clips=clips,
                               gaps=e.gaps(index)))
        return dict(id=t["id"], name=t["name"], fps=t["fps"], width=t["width"], height=t["height"],
                    currentFrame=e.current_frame, canGenerate=False, tracks=tracks, markers=t.get("markers", []))

    def call(self, name, args):
        if name not in SCHEMAS:
            known = name in ORIGINAL_TOOLS
            raise ValueError(("Unsupported original tool" if known else "Unknown tool") + f": {name}. No changes made.")
        Draft202012Validator(SCHEMAS[name]).validate(args)
        if any(isinstance(v, float) and not math.isfinite(v) for v in args.values()):
            raise ValueError("Nonfinite values are not allowed")
        e = self.editor
        if name == "manage_project":
            return self.project(args)
        if name == "import_media":
            source = Path(args["source"]["path"])
            if not source.is_absolute():
                raise ValueError("source.path must be absolute")
            with e.lock:
                document_id = e.doc["timelines"][0]["id"]
            entry = probe(source)
            entry["name"] = args.get("name", entry["name"])
            with e.lock:
                if e.doc["timelines"][0]["id"] != document_id:
                    raise ValueError("Import cancelled: active project changed")
                e.import_entry(entry)
            return dict(mediaRef=entry["id"], status="ready")
        if name == "capture_frame":
            return self.capture_frame(args)
        if name == "remove_silence":
            return self.remove_silence(args)
        if name == "list_models":
            return self.list_models()
        if name == "send_feedback":
            return self.record_feedback(args.get("kind", ""), args.get("message", ""))
        with e.lock:
            if name in ("get_timeline", "inspect_timeline"):
                return self.timeline_payload(e, args)
            if name == "manage_markers":
                if not any(k in args for k in ("create", "update", "remove")):
                    t = copy.deepcopy(e.timeline)
                    return dict(markers=t.get("markers", []))
                return e.manage_markers(args.get("create"), args.get("update"), args.get("remove"))
            if name == "get_media":
                if "folder" in args and args["folder"]:
                    assets = [m for m in e.manifest["entries"] if m.get("folder") == args["folder"]]
                else:
                    assets = list(e.manifest["entries"])
                out = []
                for m in assets:
                    if "ids" in args and m["id"] not in args["ids"]:
                        continue
                    if args.get("pending") and not m.get("generationStatus"):
                        continue
                    item = {k: m[k] for k in ("id", "name", "type", "duration", "sourceWidth", "sourceHeight", "hasAudio", "folder") if k in m}
                    try:
                        item["missing"] = not e.media_path(m).is_file()
                    except ValueError:
                        item["missing"] = True
                    out.append(item)
                return dict(assets=out, timelines=[dict(id=t["id"], name=t["name"]) for t in e.doc["timelines"]],
                            folders=e.manifest.get("folders", []))
            if name == "inspect_media":
                out = []
                for mid in args["ids"]:
                    m = e.media(mid)
                    if not m:
                        raise ValueError("Unknown media id: " + mid)
                    item = dict(m)
                    try:
                        item["missing"] = not e.media_path(m).is_file()
                        item["absolutePath"] = str(e.media_path(m))
                    except ValueError as err:
                        item["missing"] = True
                        item["error"] = str(err)
                    out.append(item)
                return dict(assets=out)
            if name == "search_media":
                q = args["query"].lower()
                hits = [m for m in e.manifest["entries"] if q in m.get("name", "").lower()]
                limit = args.get("limit", 20)
                return dict(query=args["query"], count=len(hits),
                            assets=[dict(id=m["id"], name=m["name"], type=m["type"], duration=m.get("duration")) for m in hits[:limit]])
            if name == "organize_media":
                def action():
                    folders = e.manifest.setdefault("folders", [])
                    for f in args.get("createFolders", []):
                        if f not in folders:
                            folders.append(f)
                    for r in args.get("rename", []):
                        if r["folder"] not in folders:
                            raise ValueError("Unknown folder: " + r["folder"])
                        folders.remove(r["folder"])
                        folders.append(r["name"])
                        for m in e.manifest["entries"]:
                            if m.get("folder") == r["folder"]:
                                m["folder"] = r["name"]
                    for mv in args.get("move", []):
                        for mid in mv["ids"]:
                            m = e.media(mid)
                            if not m:
                                raise ValueError("Unknown media id: " + mid)
                            m["folder"] = mv.get("folder")
                    return dict(folders=list(folders))
                return e.mutate("Organize media", action)
            if name == "insert_clips":
                return e.insert_clips(args["atFrame"], args["entries"])
            if name == "ripple_delete_ranges":
                return e.ripple_delete_ranges(args["ranges"])
            if name == "swap_clip_media":
                return e.swap_clip_media(args["swaps"])
            if name == "copy_clip_settings":
                return e.copy_clip_settings(args["sourceClipId"], args.get("targetClipIds"),
                                            args.get("targetTrack"), args.get("startFrame"), args.get("endFrame"))
            if name == "set_keyframes":
                return e.set_keyframes(args["clipId"], args["tracks"])
            if name == "apply_layout":
                return e.apply_layout(args["layout"], args["slots"], args.get("fit", "cover"))
            if name == "sync_clips":
                return e.sync_clips(args["clipIds"], args.get("toFrame"))
            if name == "manage_clip_links":
                if not args:
                    groups = {}
                    for tr in e.timeline["tracks"]:
                        for c in tr["clips"]:
                            g = c.get("linkGroupId")
                            if g:
                                groups.setdefault(g, []).append(c["id"])
                    return dict(groups=groups)
                return e.manage_clip_links(args.get("link"), args.get("unlink"))
            if name == "add_captions":
                return e.add_captions(args["cues"], args.get("trackIndex"), args.get("style"))
            if name == "apply_color":
                return e.apply_color(args["clipIds"], args.get("color"), args.get("reset", False))
            if name == "apply_effect":
                return e.apply_effect(args["clipIds"], args["effects"])
            if name == "inspect_color":
                if "clipId" in args:
                    _, c = e.clip(args["clipId"])
                    return dict(clipId=args["clipId"], color=copy.deepcopy(c.get("color") or {}),
                                effects=copy.deepcopy(c.get("effects") or []),
                                hint="Tune with apply_color knobs: exposure/contrast/saturation/temperature/highlights/shadows.")
                m = e.media(args.get("mediaRef", ""))
                if not m:
                    raise ValueError("Supply clipId or a known mediaRef")
                return dict(mediaRef=m["id"], name=m["name"], type=m["type"],
                            hint="Place the asset, then grade the clip with apply_color.")
            if name == "denoise_audio":
                return e.set_denoise(args["clipIds"], args.get("enabled", True), args.get("strength", 0.6))
            if name == "get_transcript":
                return dict(words=[], note="Local speech transcription is not bundled in this Linux port. Use captions via add_captions or attach an SRT as captions.")
            if name == "detect_beats":
                return dict(beats=[], note="Beat detection is not bundled. Cut on markers or use remove_silence for dead air.")
            if name == "manage_multicam":
                def action():
                    if args["action"] == "create":
                        group = uid()
                        for cid in args.get("clipIds", []):
                            _, c = e.clip(cid)
                            c["multicamGroupId"] = group
                        return dict(groupId=group, clipIds=args.get("clipIds", []))
                    for tr in e.timeline["tracks"]:
                        for c in tr["clips"]:
                            if c.get("multicamGroupId") == args.get("groupId"):
                                c.pop("multicamGroupId", None)
                    return dict(disbanded=args.get("groupId"))
                return e.mutate("Manage multicam", action)
            if name == "change_cam":
                def action():
                    touched = 0
                    for tr in e.timeline["tracks"]:
                        for c in tr["clips"]:
                            if c.get("multicamGroupId") == args["groupId"] and args["startFrame"] <= c["startFrame"] < args.get("endFrame", 10**9):
                                c["multicamAngle"] = args["angle"]
                                touched += 1
                    return dict(switched=touched)
                return e.mutate("Change cam", action)
            if name == "get_multicam":
                out = []
                for tr in e.timeline["tracks"]:
                    for c in tr["clips"]:
                        if c.get("multicamGroupId") == args.get("groupId"):
                            out.append(dict(clipId=c["id"], angle=c.get("multicamAngle", 0),
                                            frames=[c["startFrame"], end(c)]))
                return dict(groupId=args.get("groupId"), clips=out)
            if name in ("generate_video", "generate_image", "generate_audio", "upscale_media"):
                return dict(canGenerate=False, status="refused",
                            note="Generation/upscale needs a Palmier subscription backend, not included in this GPL Linux port. Timeline editing, color, effects and export work offline.")
            if name == "add_clips":
                return e.add_clips(args["entries"])
            if name == "move_clips":
                return e.move(args["moves"])
            if name == "remove_clips":
                return e.remove(args["clipIds"])
            if name == "split_clips":
                return e.split(args["splits"])
            if name == "set_clip_properties":
                return e.properties(args["clipIds"], {k:v for k,v in args.items() if k != "clipIds"})
            if name == "add_texts":
                return e.add_texts(args["entries"])
            if name == "update_text":
                def action():
                    if not ("content" in args or "style" in args):
                        raise ValueError("No text change supplied")
                    for cid in args["clipIds"]:
                        _, c = e.clip(cid)
                        if c["mediaType"] != "text":
                            raise ValueError("Target is not a text clip")
                        if "content" in args:
                            if not isinstance(args["content"], str) or len(args["content"]) > 10000:
                                raise ValueError("Text must be a string up to 10000 characters")
                            c["textContent"] = args["content"]
                        if "style" in args:
                            style = args["style"]
                            if "fontSize" in style:
                                from .model import number
                                number(style["fontSize"], "fontSize", 12, 300)
                            if "color" in style:
                                import re
                                if not re.fullmatch(r"#[0-9a-fA-F]{6}", style["color"]):
                                    raise ValueError("Text color must be #RRGGBB")
                            c.setdefault("textStyle", {}).update(style)
                    return dict(clipIds=args["clipIds"])
                return e.mutate("Update text", action)
            if name in ("undo", "windows_redo", "linux_redo"):
                return e.undo(name in ("windows_redo", "linux_redo"))
            if name == "manage_tracks":
                def select(selector):
                    if ("trackId" in selector) == ("index" in selector):
                        raise ValueError("Use exactly one trackId or index")
                    if "index" in selector:
                        if selector["index"] >= len(e.timeline["tracks"]):
                            raise ValueError("Unknown track index")
                        return e.timeline["tracks"][selector["index"]]
                    tr = next((tr for tr in e.timeline["tracks"] if tr["id"] == selector["trackId"]), None)
                    if tr is None:
                        raise ValueError("Unknown trackId")
                    return tr
                def action():
                    if not args:
                        raise ValueError("Nothing to do")
                    for item in args.get("reorder", []):
                        tr = select(item)
                        if item["to"] >= len(e.timeline["tracks"]) or e.timeline["tracks"][item["to"]]["type"] != tr["type"]:
                            raise ValueError("Reorder must stay inside its video/audio zone")
                        e.timeline["tracks"].remove(tr)
                        e.timeline["tracks"].insert(item["to"], tr)
                    for item in args.get("set", []):
                        tr = select(item)
                        values = {k:v for k,v in item.items() if k not in ("trackId", "index")}
                        if not values:
                            raise ValueError("No track properties specified")
                        tr.update(values)
                    for item in args.get("remove", []):
                        tr = select(dict(index=item) if type(item) is int else item)
                        e.timeline["tracks"].remove(tr)
                    return dict(status="updated")
                return e.mutate("Manage tracks", action)
            if name == "create_timeline":
                def action():
                    t = new_timeline(args.get("name", "Timeline"), e.timeline["fps"], e.timeline["width"], e.timeline["height"])
                    e.doc["timelines"].append(t)
                    e.doc["activeTimelineId"] = t["id"]
                    return dict(timelineId=t["id"])
                return e.mutate("Create timeline", action)
            if name == "set_active_timeline":
                if args["timelineId"] not in {t["id"] for t in e.doc["timelines"]}:
                    raise ValueError("Unknown timelineId")
                return e.mutate("Activate timeline", lambda: e.doc.update(activeTimelineId=args["timelineId"]))
            if name == "set_project_settings":
                if set(args) != {"width", "height"}:
                    raise ValueError("Supply both width and height; frame-rate rescaling is not implemented")
                return e.mutate("Canvas settings", lambda: e.timeline.update(args))
            if name == "export_project":
                return self.exports.start(args)
            if name == "manage_exports":
                if args["action"] == "list":
                    return dict(exports=self.exports.list())
                self.exports.cancel(args.get("jobId"))
                return dict(status="cancelling", jobId=args["jobId"])
            if name in ("windows_playback", "linux_playback"):
                if args["action"] == "seek" and "frame" not in args:
                    raise ValueError("seek requires frame")
                total = duration(e.timeline)
                if args["action"] != "pause" and not total:
                    raise ValueError("The timeline is empty")
                if args["action"] == "seek" and args["frame"] >= total:
                    raise ValueError("frame must be inside the timeline")
                self.playback(args["action"], args.get("frame", e.current_frame))
                return dict(status="requested")
        raise ValueError("Unsupported operation")

    def capture_frame(self, args):
        from .media import capture_frame, frozen_editor
        e = self.editor
        with e.lock:
            frame = args.get("frame", e.current_frame)
            total = duration(e.timeline)
            if not total:
                raise ValueError("The timeline is empty")
            if type(frame) is not int or not 0 <= frame < total:
                raise ValueError("frame must be inside the timeline")
            snapshot = frozen_editor(e)
            timeline = next(t for t in snapshot.doc["timelines"] if t["id"] == snapshot.doc["activeTimelineId"])
        if args.get("outputPath"):
            dest = Path(args["outputPath"]).expanduser().resolve()
        else:
            dest = Path(tempfile.gettempdir()) / f"palmier-frame-{uid()}.png"
        if dest.suffix.lower() != ".png":
            raise ValueError("outputPath must end with .png")
        capture_frame(snapshot, timeline, frame, dest)
        return dict(path=str(dest), frame=frame)

    def remove_silence(self, args):
        from .media import detect_silence, timeline_silence_ranges
        e = self.editor
        with e.lock:
            index = args.get("trackIndex", 0)
            if type(index) is not int or index >= len(e.timeline["tracks"]):
                raise ValueError("Unknown trackIndex")
            fps = e.timeline["fps"]
            jobs = []
            for c in e.timeline["tracks"][index]["clips"]:
                m = e.media(c["mediaRef"])
                if not m or m.get("type") not in ("audio", "video"):
                    continue
                if m.get("type") != "audio" and not m.get("hasAudio"):
                    continue
                try:
                    path = e.media_path(m)
                except ValueError:
                    continue
                if path.is_file():
                    jobs.append((copy.deepcopy(c), path))
            threshold = args.get("thresholdDb", -35)
            minimum = args.get("minLength", 0.5)
        spans = []
        min_frames = max(1, round(fps * 0.2))
        for clip, path in jobs:
            for span in timeline_silence_ranges(clip, detect_silence(path, threshold, minimum), fps, index, min_frames):
                spans.append(span)
        if not spans:
            return dict(removed=[], note="No silence spans found with current threshold")
        return e.ripple_delete_ranges(spans)

    def list_models(self):
        from .media import hw_encoders
        return dict(canGenerate=False, localFilters=["grayscale", "blur", "sharpen", "vignette", "mirror", "denoise-light"],
                    colorKnobs=["exposure", "contrast", "saturation", "temperature", "highlights", "shadows", "sharpen"],
                    hwEncoders=hw_encoders(),
                    note="Generative video/image/audio needs a Palmier subscription backend, which is not included in this GPL port.")

    def record_feedback(self, kind, message):
        if not isinstance(kind, str) or not isinstance(message, str):
            raise ValueError("Feedback kind and message must be strings")
        message = message.strip()
        if not message or len(message) > 4000:
            raise ValueError("Feedback message must be 1..4000 characters")
        root = Path(os.environ.get("PALMIER_FEEDBACK_DIR") or (Path.home() / ".local" / "share" / "palmier-pro-linux"))
        root.mkdir(parents=True, exist_ok=True)
        path = root / "feedback.jsonl"
        line = json.dumps(dict(kind=kind.strip()[:40] or "note", message=message, at=time.time()), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return dict(status="recorded", path=str(path), note="Feedback is stored on this computer only.")

    def project(self, args):
        e, action = self.editor, args["action"]
        with e.lock:
            if action == "list":
                return dict(projects=[dict(id=e.doc["timelines"][0]["id"], name=e.name, path=str(e.package) if e.package else None, active=True)])
            if action == "open" and set(args) != {"action", "path"}:
                raise ValueError("Windows currently opens projects by absolute path only")
            if action == "create" and ("id" in args or "path" in args):
                raise ValueError("create accepts name/fps/aspectRatio/quality")
            if action == "close" and set(args) != {"action"}:
                raise ValueError("Only closing the active desktop project is supported")
            if e.dirty and not e.package:
                raise ValueError("Save the active unsaved project in the desktop before switching projects")
            dirty = e.dirty
        if dirty:
            e.save()
        with e.lock:
            if e.dirty:
                raise ValueError("Project changed during save; save again before switching")
            revision = e.revision
        if action == "open":
            e.load(args["path"], expected_revision=revision)
        else:
            with e.lock:
                if e.revision != revision:
                    raise ValueError("Project changed before switching")
                if action == "create":
                    h = {"720p":720, "1080p":1080, "2K":1440, "4K":2160}[args.get("quality", "1080p")]
                    a, b = [int(x) for x in args.get("aspectRatio", "16:9").split(":")]
                    e.new(args.get("name", "Untitled"), args.get("fps",30), round(h*a/b/2)*2, h)
                else:
                    e.new()
        return dict(id=e.doc["timelines"][0]["id"], name=e.name, status=action)


class MCPServer:
    def __init__(self, tools, port=19789):
        token = os.environ.get("PALMIER_MCP_TOKEN")
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, value=None):
                data = json.dumps(value, allow_nan=False).encode("utf-8") if value is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def do_GET(self):
                self.reply(405, dict(error="Stateless MCP: use POST /mcp"))

            def do_POST(self):
                self.connection.settimeout(30)
                if self.path != "/mcp":
                    return self.reply(404)
                if self.headers.get("Origin") or self.headers.get("Host") not in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"):
                    return self.reply(403, dict(error="Browser origins and non-loopback hosts are refused"))
                if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    return self.reply(401)
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.reply(415)
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 1_048_576:
                        return self.reply(413)
                    request = json.loads(self.rfile.read(length), parse_constant=lambda s: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                        raise ValueError("Invalid JSON-RPC envelope")
                except Exception:
                    return self.reply(400, dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message="Malformed JSON-RPC request")))
                if "id" not in request:
                    return self.reply(202)
                rid = request["id"]
                method, params = request.get("method"), request.get("params", {})
                if method == "initialize":
                    result = dict(protocolVersion=params.get("protocolVersion", "2025-03-26"),
                                  capabilities=dict(tools=dict(listChanged=False)),
                                  serverInfo=dict(name="palmier-pro-linux", version=VERSION),
                                  instructions="Native Qt Linux editor. Supported GPL tool subsets only. Reads and edits share desktop state. Save unsaved projects before switching.")
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = dict(tools=tools.list())
                elif method == "tools/call":
                    try:
                        value = tools.call(params["name"], params.get("arguments", {}))
                        result = dict(content=[dict(type="text", text=json.dumps(value, allow_nan=False))], isError=False)
                    except Exception as error:
                        result = dict(content=[dict(type="text", text=str(error))], isError=True)
                else:
                    return self.reply(200, dict(jsonrpc="2.0", id=rid, error=dict(code=-32601, message="Method not found")))
                self.reply(200, dict(jsonrpc="2.0", id=rid, result=result))
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
