#!/usr/bin/env python3
"""
녹화본(webm, 실제 시간)을 단계별로 속도를 달리해 1분짜리 mp4 로 만든다.
기다리는 구간은 빨리, 자막이 바뀌는 구간은 최소 MIN_SEG 초 머물게 한다. 구간마다 따로 인코딩해 이어 붙인다 (메모리 절약).

  python3 scripts/encode_demo.py data/demo/raw/page@....webm data/demo/marks.json docs/demo/demo.mp4
marks.json: {"marks": [단계가 바뀐 시각(녹화 시작부터 초), ...], "end": 녹화 길이}
"""
import json
import os
import subprocess
import sys
import tempfile


def main():
    raw, marks_path, out = sys.argv[1:4]
    target = float(os.environ.get("TARGET_SECONDS", 60))
    min_seg = float(os.environ.get("MIN_SEG", 5))
    m = json.load(open(marks_path))
    bounds = [0.0]
    for t in m["marks"][1:] + [m["end"]]:
        if t - bounds[-1] > 0.3:
            bounds.append(t)
    segs = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    spare = max(0.0, target - min_seg * len(segs))
    total = sum(e - s for s, e in segs)
    tmp = tempfile.mkdtemp(prefix="demo-")
    files = []
    for i, (s, e) in enumerate(segs):
        real = e - s
        dur = min(real, min_seg + spare * real / total)
        f = os.path.join(tmp, f"{i:02d}.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{s:.3f}", "-t", f"{real:.3f}", "-i", raw,
                        "-filter:v", f"setpts=PTS/{real / dur:.4f},fps=30", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-crf", "28", "-threads", "2", f], check=True)
        files.append(f)
        print(f"  segment {i}: {real:6.1f}s → {dur:4.1f}s (x{real / dur:.1f})", flush=True)
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as fh:
        fh.writelines(f"file '{f}'\n" for f in files)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                    "-movflags", "+faststart", out], check=True)
    print("wrote", out)


if __name__ == "__main__":
    main()
