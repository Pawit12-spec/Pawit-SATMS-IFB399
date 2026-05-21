"""
Zone Mapper — draw equipment exclusion zones over a thermal image.

Saves zones as arrays of pixel coordinates (in original image space) to a JSON
file. Supports both rectangle and polygon drawing so irregular equipment shapes
can be outlined accurately.

Controls:
  r           : switch to rectangle mode  (click and drag)
  p           : switch to polygon mode    (click points, right-click or c to close)
  n           : set a new zone name       (prompts in terminal)
  u           : undo last polygon point
  c / Enter   : close current polygon
  d           : delete last completed zone
  s           : save zones to JSON and quit
  q / Esc     : quit without saving

Usage:
    python zone_mapper.py path/to/image.png
    python zone_mapper.py path/to/image.png --output my_zones.json
    python zone_mapper.py path/to/image.png --scale 15
"""

import os
import sys
import json
import argparse
import cv2
import numpy as np

DEFAULT_SCALE = 12

COLOURS = [
    (0,   200,   0),   # green
    (0,   200, 200),   # yellow
    (200,   0, 200),   # magenta
    (200, 200,   0),   # cyan
    (0,   100, 255),   # orange
    (255, 100,   0),   # blue
]


class ZoneMapper:
    def __init__(self, image_path, output_path, scale):
        self.image_path  = image_path
        self.output_path = output_path
        self.scale       = scale

        raw = cv2.imread(image_path)
        if raw is None:
            print(f"[ERROR] Could not load: {image_path}")
            sys.exit(1)

        self.orig_h, self.orig_w = raw.shape[:2]
        self.disp_w = self.orig_w * scale
        self.disp_h = self.orig_h * scale
        self.base   = cv2.resize(raw, (self.disp_w, self.disp_h),
                                 interpolation=cv2.INTER_NEAREST)

        # completed zones — each has name, mode, points (in original image pixels)
        self.zones      = []
        self.mode       = "rect"
        self.zone_name  = "Zone_1"

        # rectangle state
        self.rect_start = None
        self.drawing    = False

        # polygon state
        self.poly_pts   = []
        self.cursor     = (0, 0)

        self.win = "Zone Mapper  |  [r]ect  [p]oly  [n]ame  [u]ndo  [d]el  [c]lose  [s]ave  [q]uit"
        cv2.namedWindow(self.win, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.win, min(self.disp_w + 10, 1400), min(self.disp_h + 50, 900))
        cv2.setMouseCallback(self.win, self._mouse)

    # ── coordinate helpers ────────────────────────────────────────────────────

    def _to_orig(self, x, y):
        """Convert display pixel → original image pixel."""
        return round(x / self.scale, 2), round(y / self.scale, 2)

    def _to_disp(self, x, y):
        """Convert original image pixel → display pixel."""
        return int(x * self.scale), int(y * self.scale)

    # ── mouse callback ────────────────────────────────────────────────────────

    def _mouse(self, event, x, y, flags, _):
        self.cursor = (x, y)

        if self.mode == "rect":
            if event == cv2.EVENT_LBUTTONDOWN:
                self.drawing    = True
                self.rect_start = (x, y)
            elif event == cv2.EVENT_LBUTTONUP and self.drawing:
                self.drawing = False
                x1d, y1d = min(self.rect_start[0], x), min(self.rect_start[1], y)
                x2d, y2d = max(self.rect_start[0], x), max(self.rect_start[1], y)
                if abs(x2d - x1d) > 2 and abs(y2d - y1d) > 2:
                    # store corners in original image pixel space
                    pts = [
                        list(self._to_orig(x1d, y1d)),
                        list(self._to_orig(x2d, y1d)),
                        list(self._to_orig(x2d, y2d)),
                        list(self._to_orig(x1d, y2d)),
                    ]
                    self._commit_zone(pts, "rect")
                self.rect_start = None

        elif self.mode == "poly":
            if event == cv2.EVENT_LBUTTONDOWN:
                ox, oy = self._to_orig(x, y)
                self.poly_pts.append([ox, oy])
                print(f"    point {len(self.poly_pts)}: ({ox}, {oy})")
            elif event == cv2.EVENT_RBUTTONDOWN:
                self._close_polygon()

    # ── zone management ───────────────────────────────────────────────────────

    def _commit_zone(self, points, mode):
        self.zones.append({"name": self.zone_name, "mode": mode, "points": points})
        print(f"  [+] '{self.zone_name}' saved  ({mode}, {len(points)} pts)")
        self._auto_next_name()

    def _close_polygon(self):
        if len(self.poly_pts) < 3:
            print("  [!] Need at least 3 points — keep clicking")
            return
        self._commit_zone([list(p) for p in self.poly_pts], "poly")
        self.poly_pts = []

    def _auto_next_name(self):
        parts = self.zone_name.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            self.zone_name = f"{parts[0]}_{int(parts[1]) + 1}"

    # ── drawing ───────────────────────────────────────────────────────────────

    def _render(self):
        frame = self.base.copy()

        # completed zones
        for i, zone in enumerate(self.zones):
            colour = COLOURS[i % len(COLOURS)]
            pts_disp = np.array(
                [self._to_disp(p[0], p[1]) for p in zone["points"]], dtype=np.int32
            )
            overlay = frame.copy()
            cv2.fillPoly(overlay, [pts_disp], colour)
            cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, frame)
            cv2.polylines(frame, [pts_disp], isClosed=True, color=colour, thickness=1)
            cx = int(np.mean(pts_disp[:, 0]))
            cy = int(np.mean(pts_disp[:, 1]))
            cv2.putText(frame, zone["name"], (cx - 20, cy + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, colour, 1)

        # rectangle in progress
        if self.mode == "rect" and self.drawing and self.rect_start:
            cv2.rectangle(frame, self.rect_start, self.cursor, (0, 220, 0), 1)

        # polygon in progress
        if self.mode == "poly" and self.poly_pts:
            pts_disp = [self._to_disp(p[0], p[1]) for p in self.poly_pts]
            for j in range(len(pts_disp) - 1):
                cv2.line(frame, pts_disp[j], pts_disp[j + 1], (0, 220, 0), 1)
            cv2.line(frame, pts_disp[-1], self.cursor, (0, 220, 0), 1)
            for pt in pts_disp:
                cv2.circle(frame, pt, 2, (0, 255, 0), -1)

        # HUD bar
        mode_str = "RECTANGLE" if self.mode == "rect" else "POLYGON"
        hint = ("drag to draw" if self.mode == "rect"
                else "click pts · right-click or [c] to close" if not self.poly_pts
                else f"{len(self.poly_pts)} pt(s) · right-click or [c] to close · [u] undo")
        line1 = f"Mode: {mode_str}   Zone: {self.zone_name}   Saved: {len(self.zones)}   {hint}"
        cv2.rectangle(frame, (0, 0), (self.disp_w, 34), (30, 30, 30), -1)
        cv2.putText(frame, line1, (5, 13),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (210, 210, 210), 1)
        cv2.putText(frame, "[r]ect  [p]oly  [n]ame  [u]ndo  [d]el last  [c]lose poly  [s]ave  [q]uit",
                    (5, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.30, (140, 140, 140), 1)

        return frame

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        print(f"\nImage : {self.image_path}  ({self.orig_w}×{self.orig_h}  →  displayed at {self.scale}×)")
        print(f"Output: {self.output_path}")
        print(f"Zone  : '{self.zone_name}'  (change with [n])\n")

        while True:
            cv2.imshow(self.win, self._render())
            key = cv2.waitKey(20) & 0xFF

            if key == ord('r'):
                self.mode     = "rect"
                self.poly_pts = []
                print("  Mode → RECTANGLE")

            elif key == ord('p'):
                self.mode    = "poly"
                self.drawing = False
                print("  Mode → POLYGON")

            elif key == ord('n'):
                name = input(f"  Zone name (current: '{self.zone_name}'): ").strip()
                if name:
                    self.zone_name = name
                    print(f"  Name set to '{self.zone_name}'")

            elif key == ord('u'):
                if self.mode == "poly" and self.poly_pts:
                    removed = self.poly_pts.pop()
                    print(f"  Undo — removed {removed}  ({len(self.poly_pts)} left)")
                elif self.mode == "rect":
                    print("  [u] only applies in polygon mode")

            elif key in (ord('c'), 13):   # c or Enter
                if self.mode == "poly":
                    self._close_polygon()

            elif key == ord('d'):
                if self.zones:
                    removed = self.zones.pop()
                    print(f"  Deleted '{removed['name']}'")
                else:
                    print("  No zones to delete")

            elif key == ord('s'):
                self._save()
                break

            elif key in (ord('q'), 27):   # q or Esc
                print("  Quit — nothing saved.")
                break

        cv2.destroyAllWindows()

    # ── save ──────────────────────────────────────────────────────────────────

    def _save(self):
        out = {
            "image":        os.path.basename(self.image_path),
            "image_size":   {"width": self.orig_w, "height": self.orig_h},
            "zones": [
                {"name": z["name"], "mode": z["mode"], "points": z["points"]}
                for z in self.zones
            ]
        }
        with open(self.output_path, "w") as f:
            json.dump(out, f, indent=2)

        print(f"\n  Saved {len(self.zones)} zone(s) → {self.output_path}\n")
        for z in self.zones:
            print(f"    {z['name']:20s}  {z['mode']:4s}  {len(z['points'])} points")
            for pt in z["points"]:
                print(f"                          {pt}")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image",           help="Path to thermal image")
    parser.add_argument("--output", "-o",  help="Output JSON path (default: zones.json next to image)")
    parser.add_argument("--scale",  "-s",  type=int, default=DEFAULT_SCALE,
                        help=f"Display scale for small images (default: {DEFAULT_SCALE})")
    args = parser.parse_args()

    if not os.path.exists(args.image):
        print(f"[ERROR] Image not found: {args.image}")
        sys.exit(1)

    output = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.image)), "zones.json"
    )
    ZoneMapper(args.image, output, args.scale).run()


if __name__ == "__main__":
    main()
