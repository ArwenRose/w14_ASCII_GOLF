"""ASCII GOLF v0.2 - a nine-hole terminal golf game.

Python 3.10+, standard library only. Designed for Windows Terminal and
VS Code's integrated terminal (interactive console required).

Coordinates and distances are yards; wind components are metres/second.
The shot model is intentionally simplified for gameplay, not a golf simulator.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import select
import shutil
import sys
from dataclasses import dataclass

W, H = 59, 23
GREEN_RADIUS = 9.0
CUP_RADIUS = 0.34
# club: maximum baseline carry in yards, loft in degrees, dispersion in degrees
CLUBS = {
    "1W": (225.0, 11.0, 7.0), "3W": (195.0, 15.0, 6.0),
    "5I": (165.0, 24.0, 4.5), "7I": (140.0, 34.0, 3.5),
    "9I": (115.0, 42.0, 3.0), "PW": (85.0, 49.0, 2.5),
    "SW": (55.0, 57.0, 2.0), "PT": (13.0, 0.0, 0.0),
}
# Debug-only prototype options; ordinary players cannot change these in a round.
GEAR = {"BALANCED": (1.0, 1.0), "DISTANCE": (1.08, 1.35), "CONTROL": (.94, .65)}
BALLS = {"STANDARD": (1.0, 1.0), "TOUR": (1.04, .8), "SOFT": (.96, 1.3)}
ZOOM_NAMES = ("COURSE ALL", "COURSE MID", "COURSE CLOSE", "GREEN ALL", "GREEN CLOSE")


@dataclass(frozen=True)
class Hole:
    name: str
    par: int
    length: float
    seed: int
    kind: str
    wind: tuple[float, float]  # (eastward, cupward) metres per second
    wet: float


HOLES = [
    Hole("STARTER STRAIGHT", 4, 300, 11, "straight", (1.2, -.5), 0.0),
    Hole("RIVERSIDE", 4, 320, 22, "river", (1.4, .7), .15),
    Hole("LEFT DOGLEG", 4, 345, 33, "dogleg", (-2.5, .4), 0),
    Hole("POND CARRY", 3, 165, 44, "pond", (1.5, -1.2), .1),
    Hole("FOREST PASS", 4, 305, 55, "forest", (-1.2, 1.3), 0),
    Hole("HILLTOP", 3, 145, 66, "hill", (2.5, 0), 0),
    Hole("SNAKE GREEN", 4, 290, 77, "snake", (.5, -1), .1),
    Hole("STORMY LINKS", 5, 425, 88, "links", (4.2, 1.5), .4),
    Hole("FINAL VALLEY", 5, 445, 99, "river", (-1.8, -1), 0),
]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d else (0., 0.)


def aim_at_cup(x, y, h):
    """Angle 0 points up the map; clockwise angles are positive."""
    return math.degrees(math.atan2(-x, h.length - y)) % 360


def terrain(hole: Hole, x: float, y: float) -> str:
    """Tee=(0, 0), cup=(0, hole.length), yards."""
    if y < -15 or y > hole.length + 24 or abs(x) > 95:
        return "OB"
    center = (-27 * math.sin(math.pi * clamp(y / hole.length, 0, 1))
              if hole.kind == "dogleg" else 3 * math.sin(y / 65))
    if hole.kind == "river" and 17 < x < 29:
        return "WATER"
    if hole.kind == "pond" and hole.length * .38 < y < hole.length * .64 and abs(x) < 26:
        return "WATER"
    if distance((x, y), (0, hole.length)) < GREEN_RADIUS:
        return "GREEN"
    if abs(x - center) < (7 if hole.kind == "forest" else 13):
        return "FAIRWAY"
    if abs(x - center) < 21:
        return "ROUGH"
    if hole.kind in ("forest", "dogleg") and abs(x) < 65:
        return "WOODS"
    if hole.kind == "links" and (x > 39 or x < -45):
        return "BUNKER"
    return "ROUGH"


def height(hole: Hole, x: float, y: float) -> float:
    """Continuously generated terrain height (yards, arbitrary reference)."""
    z = 1.6 * math.sin(y / 24) * math.cos(x / 18) + .7 * math.sin(x / 11 + y / 17)
    if hole.kind == "hill":
        z += 10 * clamp(y / hole.length, 0, 1)
    if hole.kind == "snake":
        z += 2.1 * math.sin((y - hole.length) / 4) * math.exp(-abs(x) / 16)
    dy = y - hole.length
    if abs(dy) < 13 and abs(x) < 13:
        z += .33 * math.sin(x / 2.8 + dy / 3.5) + .22 * math.cos(dy / 2.2)
    return z


def gradient(hole, x, y):
    e = .12
    return ((height(hole, x + e, y) - height(hole, x - e, y)) / (2 * e),
            (height(hole, x, y + e) - height(hole, x, y - e)) / (2 * e))


def grain(hole, x, y):
    a = .8 * math.sin((y - hole.length) / 5 + hole.seed * .1) + .5 * math.cos(x / 5)
    return (.18 * math.cos(a), .18 * math.sin(a))


def wind_drift(hole: Hole, loft: float, peak_yards: float) -> tuple[float, float]:
    """Wind displacement at landing in yards.

    Approximate time aloft from apex height; taller/higher-loft shots expose
    more of the ball to wind. Both head/tailwind and crosswind are included.
    Not an aerodynamic spin/drag solver.
    """
    seconds = math.sqrt(8 * max(1., peak_yards) * .9144 / 9.81)
    sensitivity = .36 + .014 * loft + .12 * clamp(peak_yards / 15, 0, 1)
    return (hole.wind[0] * seconds * 1.09361 * sensitivity,
            hole.wind[1] * seconds * 1.09361 * sensitivity)


class Game:
    def __init__(self, seed=20261117, debug=False, difficulty="Normal"):
        self.difficulty = difficulty
        self.rng = random.Random(seed)
        self.debug = debug
        self.hole_index = 0
        self.gear = "BALANCED"
        self.ball_type = "STANDARD"
        self.total = 0
        self.scores = []
        self.shot_history = []
        self.phase = "PLAY"
        self.zoom = 0
        self.overlay = "GRAIN"  # Change with L and G on green zooms.
        self.angle = 0.0
        self.power = 75
        self.club = "7I"
        self.x = self.y = 0.0
        self.strokes = 0
        self.message = "Choose a club and power, then press SPACE."
        self.last_path = []
        self.undo_state = None
        self.start_hole()

    @property
    def hole(self):
        return HOLES[self.hole_index]

    def start_hole(self):
        self.x, self.y = 0.0, 0.0
        self.strokes = 0
        self.angle, self.power, self.club = aim_at_cup(0, 0, self.hole), 75, "7I"
        self.phase = "PLAY"
        self.zoom = 0  # Show the entire hole before choosing a tee shot.
        self.overlay = "GRAIN"
        self.last_path = []
        self.undo_state = None
        self.message = f"HOLE {self.hole_index + 1}: {self.hole.name} - PAR {self.hole.par}"

    def near_green(self):
        return self.remaining() < GREEN_RADIUS + 2

    def remaining(self):
        return distance((self.x, self.y), (0, self.hole.length))

    def club_cycle(self, delta=1):
        keys = list(CLUBS)
        self.club = keys[(keys.index(self.club) + delta) % len(keys)]

    def baseline_carry(self):
        base, _, _ = CLUBS[self.club]
        dist_mult, _ = GEAR[self.gear]
        ball_mult, _ = BALLS[self.ball_type]
        return base * self.power / 100 * dist_mult * ball_mult

    def baseline_target(self):
        """Reference marker (+): straight flight, NO wind, NO random dispersion."""
        d = self.baseline_carry()
        a = math.radians(self.angle)
        return self.x + math.sin(a) * d, self.y + math.cos(a) * d

    def _save_undo(self):
        if self.debug:
            self.undo_state = (self.hole_index, self.x, self.y, self.strokes,
                               self.angle, self.power, self.club, self.phase,
                               self.zoom, self.overlay, list(self.last_path),
                               self.total, list(self.scores), self.gear, self.ball_type,
                               self.rng.getstate())

    def undo(self):
        if not self.debug or self.undo_state is None:
            return
        (index, self.x, self.y, self.strokes, self.angle, self.power,
         self.club, self.phase, self.zoom, self.overlay,
         self.last_path, self.total, self.scores, self.gear,
         self.ball_type, rng_state) = self.undo_state
        if index != self.hole_index:
            self.undo_state = None
            return
        self.rng.setstate(rng_state)
        self.undo_state = None  # Undo only one shot; no multi-step history.
        self.message = "DEBUG: last shot restored (including random state)."

    def hit(self):
        if self.phase != "PLAY":
            return
        if self.zoom >= 3 and self.overlay == "LEVEL":
            self.message = "Switch to GRAIN (G) before shooting."
            return
            self._save_undo()

        start = (self.x, self.y)
        club = self.club
        power = self.power

        if self.club == "PT":
            self._putt()
        else:
            self._full_shot()

        self.record_shot(start, club, power)
        if self.phase == "PLAY":
            self.angle = aim_at_cup(self.x, self.y, self.hole)
            if self.near_green():
                self.zoom = max(self.zoom, 3)
                if self.club != "PT":
                    self.club = "PT"
                    self.power = 40
            else:
                if self.club == "PT":
                    self.club = "SW"
                    self.zoom = min(self.zoom, 2)
                elif self.zoom == 0:
                    # Tee starts with course overview; then follow the ball.
                    self.zoom = 1
    def record_shot(self, start, club, power):
        travelled = distance(start, (self.x, self.y))

        shot = {
            "hole": self.hole_index + 1,
            "shot": self.strokes,
            "club": club,
            "power": power,
            "distance": round(travelled, 1)
        }

        self.shot_history.append(shot)
        def show_shot_history(self):
            if not self.shot_history:
                self.message = "No shot history yet."
            return

        recent = self.shot_history[-1]
        self.message = (
            f"SHOT HISTORY | Hole {recent['hole']} "
            f"| Club {recent['club']} "
            f"| Power {recent['power']}% "
            f"| Distance {recent['distance']}yd"
        )
    def _full_shot(self):
        h = self.hole
        start = (self.x, self.y)
        _, loft, spread = CLUBS[self.club]
        _, accuracy = GEAR[self.gear]
        _, ball_roll = BALLS[self.ball_type]
        p = self.power / 100
        difficulty_factor = {
            "Easy": 0.5,
            "Normal": 1.0,
            "Hard": 1.5
        }

        deviation = self.rng.gauss(
            0,
            spread * accuracy * (.45 + .55 * p)
            * difficulty_factor[self.difficulty]
        )
        theta = math.radians(self.angle + deviation)
        dx, dy = math.sin(theta), math.cos(theta)
        carry = self.baseline_carry() * self.rng.uniform(.97, 1.03)
        launch = math.radians(loft * .73)
        peak = max(1.0, carry * .25 * math.tan(launch))
        wx, wy = wind_drift(h, loft, peak)
        steps = max(30, int(carry * 1.5))
        self.last_path = [start]
        landed = None
        blocked = False
        for i in range(1, steps + 1):
            t = i / steps
            bx = start[0] + dx * carry * t + wx * t * t
            by = start[1] + dy * carry * t + wy * t * t
            z = height(h, *start) + 4 * peak * t * (1 - t)
            ground = height(h, bx, by)
            typ = terrain(h, bx, by)
            if typ == "WOODS" and z < ground + 12:
                landed, blocked = (bx, by), True
                break
            if z <= ground and t > .05:
                landed = (bx, by)
                break
            if i % max(1, steps // 22) == 0:
                self.last_path.append((bx, by))
        if landed is None:
            landed = (bx, by)
        self.x, self.y = landed
        print(
    f"\nDEBUG: start={start}, landed={landed}, "
    f"carry={carry}, blocked={blocked}",
    flush=True
)
        self.strokes += 1
        typ = terrain(h, self.x, self.y)
        if typ in ("WATER", "OB"):
            self.x, self.y = start
            self.strokes += 1
            self.message = f"{typ}! +1 penalty; replay from previous lie."
            return
        if blocked:
            self.message = "TREE HIT! Ball stopped in the woods."
            return
        surface = {"FAIRWAY": .10, "ROUGH": .035, "WOODS": .012,
                   "BUNKER": .005, "GREEN": .08}.get(typ, .03)
        gx, gy = gradient(h, self.x, self.y)
        run = carry * surface * ball_roll * (1 - .65 * h.wet)
        run *= clamp(1 - 2.5 * (gx * dx + gy * dy), .25, 1.6)
        for i in range(1, 17):
            xx = landed[0] + dx * run * i / 16
            yy = landed[1] + dy * run * i / 16
            if terrain(h, xx, yy) in ("WATER", "OB"):
                self.x, self.y = start
                self.strokes += 1
                self.message = "Rolled into WATER/OB! +1 penalty; replay."
                return
            self.x, self.y = xx, yy
        self.message = (f"{self.club}: carry {distance(start, landed):.0f}yd ")
        self.message += f"(ground track), run {distance(landed, (self.x, self.y)):.0f}yd. "
        self.message += f"Lie: {terrain(h, self.x, self.y)}."

    def _putt(self):
        h = self.hole
        start = (self.x, self.y)
        theta = math.radians(self.angle)
        # Nominal flat-green roll scales approximately with selected power.
        # Matches PT max baseline carry (13yd at 100% power).
        speed = math.sqrt(2 * .26 * CLUBS["PT"][0] * self.power / 100)
        vx, vy = speed * math.sin(theta), speed * math.cos(theta)
        self.last_path = [start]
        self.strokes += 1
        for step in range(1200):
            dt = .035
            gx, gy = gradient(h, self.x, self.y)
            grx, gry = grain(h, self.x, self.y)
            vx += (-9.8 * gx * .12 + grx * .08) * dt
            vy += (-9.8 * gy * .12 + gry * .08) * dt
            vel = math.hypot(vx, vy)
            if vel < .035:
                break
            ux, uy = unit(vx, vy)
            friction = (.26 + .22 * h.wet) * (1 - .25 * (ux * grx + uy * gry))
            new_speed = max(0., vel - friction * dt)
            vx, vy = ux * new_speed, uy * new_speed
            nx, ny = self.x + vx * dt, self.y + vy * dt
            if distance((nx, ny), (0, h.length)) < CUP_RADIUS and new_speed < 1.45:
                self.x, self.y = 0., h.length
                self._finish_hole()
                return
            if terrain(h, nx, ny) in ("WATER", "OB"):
                self.x, self.y = start
                self.strokes += 1
                self.message = "Putt out of bounds! +1 penalty."
                return
            self.x, self.y = nx, ny
            if step % 12 == 0:
                self.last_path.append((self.x, self.y))
        self.message = (f"Putt travelled {distance(start, (self.x, self.y)):.1f}yd; "
                        f"{self.remaining():.1f}yd to cup.")

    def _finish_hole(self):
        h = self.hole
        self.total += self.strokes
        self.scores.append((h.name, h.par, self.strokes))
        self.phase = "FINISHED"
        self.message = f"HOLED OUT! {self.strokes} strokes (PAR {h.par}). Press N for next hole."

    def advance(self):
        if self.phase != "FINISHED":
            self.message = "Finish the hole before advancing."
            return
        if self.hole_index + 1 < len(HOLES):
            self.hole_index += 1
            self.start_hole()
        else:
            self.phase = "COMPLETE"
            par = sum(h.par for h in HOLES)
            self.message = f"ROUND COMPLETE! {self.total} strokes, {self.total-par:+d} vs PAR."
            self.undo_state = None

    def _camera(self):
        h = self.hole
        if self.zoom == 0:
            # The overall map is deliberately anisotropic: explicitly label both axes.
            return 0., h.length / 2, 4., (h.length + 35) / (H - 1)
        if self.zoom == 1:
            return self.x, self.y + 65, 7., 7.
        if self.zoom == 2:
            return self.x, self.y + 10, 1.8, 1.8
        if self.zoom == 3:
            return 0., h.length, .9, .9
        return self.x, self.y, .25, .25

    def render(self):
        if self.phase == "COMPLETE":
            par_total = sum(h.par for h in HOLES)
            lines = [
                " ASCII GOLF v0.2 | ROUND COMPLETE",
                "=" * 59,
                " HOLE  COURSE                 PAR  SHOTS  +/-",
                "-" * 59,
            ]
            lines += [
                f" {i+1:02d}    {name:<21} {par:>3}   {shots:>3}  {shots-par:+4d}"
                for i, (name, par, shots) in enumerate(self.scores)
            ]
            lines += [
                "-" * 59,
                f" TOTAL                        {par_total:>3}   {self.total:>3}  {self.total-par_total:+4d}",
                "", " Round finished. Press Q to exit.",
            ]
            return "\n".join(lines)
        h = self.hole
        cx, cy, sx, sy = self._camera()
        green_view = self.zoom >= 3
        target = self.baseline_target() if self.phase == "PLAY" else None
        grid = []
        for r in range(H):
            line = []
            for c in range(W):
                x = cx + (c - W // 2) * sx
                y = cy + (H // 2 - r) * sy
                t = terrain(h, x, y)
                ch = {"FAIRWAY": ".", "ROUGH": "w", "WOODS": "A", "WATER": "~",
                      "BUNKER": ":", "GREEN": ",", "OB": " "}.get(t, " ")
                if green_view and t == "GREEN":
                    if self.overlay == "LEVEL":
                        # Keep v0.1's visually recognizable height digits.
                        ch = str(int(round(height(h, x, y) * 10)) % 10)
                    else:
                        grx, gry = grain(h, x, y)
                        if abs(grx) > abs(gry):
                            ch = ">" if grx > 0 else "<"
                        else:
                            ch = "^" if gry > 0 else "v"
                        if (r + c) % 3:
                            ch = "."
                # Reference target is baseline carry without random/wind/terrain.
                if target and abs(x - target[0]) <= sx / 2 and abs(y - target[1]) <= sy / 2:
                    ch = "+"
                if abs(x) <= sx / 2 and abs(y - h.length) <= sy / 2:
                    ch = "O"
                if abs(x - self.x) <= sx / 2 and abs(y - self.y) <= sy / 2:
                    ch = "@"
                line.append(ch)
            # Elevation ruler: Y position in yards from tee, every fourth row.
            tick = f"  y={cy + (H // 2 - r) * sy:6.1f}yd" if r % 4 == 0 else ""
            grid.append("|" + "".join(line) + "|" + tick)
        _, loft, _ = CLUBS[self.club]
        power_bar = ("#" * (self.power // 5)).ljust(20, "-")
        delta_z = height(h, 0, h.length) - height(h, self.x, self.y)
        lines = [
            f" ASCII GOLF v0.2 | HOLE {self.hole_index+1:02d}/{len(HOLES):02d} | "
            f"{h.name} | PAR {h.par} | SHOTS {self.strokes}",
            f" VIEW {self.zoom+1}/5: {ZOOM_NAMES[self.zoom]} "
            f"| SCALE X {sx:.2f}yd/cell, Y {sy:.2f}yd/cell | y=from tee",
            "+" + "-" * W + "+", *grid, "+" + "-" * W + "+",
            f" CLUB {self.club} | MAX {CLUBS[self.club][0]:.0f}yd | LOFT {loft:.0f}deg "
            f"| AIM {self.angle:05.1f}deg | POWER {self.power:3d}%",
            f" POWER [{power_bar}] | BASE CARRY {self.baseline_carry():.0f}yd (+ on map)",
            f" TO CUP {self.remaining():6.1f}yd | CUP HEIGHT {delta_z:+.1f}yd relative to ball "
            f"| LIE {terrain(h, self.x, self.y)}",
            f" WIND X {h.wind[0]:+.1f}m/s (E+)  Y {h.wind[1]:+.1f}m/s (to cup+) "
            f"| WET {h.wet:.0%}",
        ]
        if green_view:
            gx, gy = gradient(h, self.x, self.y)
            grx, gry = grain(h, self.x, self.y)
            lines.append(f" GREEN {self.overlay}: grain ({grx:+.2f},{gry:+.2f}) "
                         f"height {height(h, self.x, self.y):+.2f}yd "
                         f"slope ({gx:+.2f},{gy:+.2f})")
        lines += [
            " Arrows +/-1deg or +/-1% | Shift+Arrows (or WASD) +/-5 | C club | SPACE hit",
            " Z zoom in | X zoom out | L level | G grain | N next | Q quit",
        ]
        if self.debug:
            lines.append(" [DEBUG] U undo last shot | E gear | B ball "
                         f"({self.gear}/{self.ball_type})")
        lines.append(" " + self.message)
        return "\n".join(lines)

    
    def show_shot_history(self):
        history = getattr(self, "shot_history", [])

        if not history:
            self.message = "No shot history yet."
            return

        recent = history[-5:]
        self.message = "Shot History: " + " | ".join(
            str(shot) for shot in recent
        )

    def command(self, key):
        if key == "SPACE":
            self.hit()
            return

        if key == "H":
            self.show_shot_history()
            return

        if self.phase == "COMPLETE":
            return

        if key == "N":
            self.advance()
            return
    
        if self.phase == "COMPLETE":
            return


        if self.phase == "FINISHED":
            if key == "U" and self.debug:
                self.undo()
            return

        if key in ("LEFT", "RIGHT", "S-LEFT", "S-RIGHT"):
            step = 5 if key.startswith("S-") else 1
            self.angle = (
                self.angle + (-step if key.endswith("LEFT") else step)
            ) % 360
            if self.phase == "COMPLETE":
                return
            if key == "N":
                self.advance()
                return
            if self.phase == "FINISHED":
                    if key == "U" and self.debug:
                        self.undo()
                        return
                    if key in ("LEFT", "RIGHT", "S-LEFT", "S-RIGHT"):
                        step = 5 if key.startswith("S-") else 1
                        self.angle = (self.angle + (-step if key.endswith("LEFT") else step)) % 360
            elif key in ("UP", "DOWN", "S-UP", "S-DOWN"):
                step = 5 if key.startswith("S-") else 1
                self.power = int(clamp(self.power + (step if key.endswith("UP") else -step), 5, 100))
            elif key in ("A", "D"):
                self.angle = (self.angle + (-5 if key == "A" else 5)) % 360
            elif key in ("W", "S"):
                self.power = int(clamp(self.power + (5 if key == "W" else -5), 5, 100))
            elif key == "C":
             self.club_cycle()
            elif key == "SPACE":
             self.message = "SPACE key detected!"
             self.hit()
             
        elif key == "Z":
            self.zoom = min(4, self.zoom + 1)
        elif key == "X":
            self.zoom = max(0, self.zoom - 1)
        elif key == "L" and self.zoom >= 3:
         self.overlay = "LEVEL" if self.overlay == "GRAIN" else "GRAIN"
        elif key == "G":
         self.overlay = "GRAIN"
        elif key == "G":
            self.overlay = "GRAIN"
        elif key == "U" and self.debug:
            self.undo()
        elif key == "E" and self.debug:
            options = list(GEAR)
            self.gear = options[
                (options.index(self.gear) + 1) % len(options)
            ]
            self.message = f"DEBUG: gear set to {self.gear}"
        elif key == "B" and self.debug:
            options = list(BALLS)
            self.ball_type = options[
                (options.index(self.ball_type) + 1) % len(options)
            ]
            self.message = f"DEBUG: ball set to {self.ball_type}"


def decode_key(sequence, shift=False):
    """Translate classic Windows special keys and ANSI CSI arrows to commands."""
    if sequence == " ":
        return "SPACE"
    if sequence in ("\x00K", "\xe0K"):
        return "S-LEFT" if shift else "LEFT"
    if sequence in ("\x00M", "\xe0M"):
        return "S-RIGHT" if shift else "RIGHT"
    if sequence in ("\x00H", "\xe0H"):
        return "S-UP" if shift else "UP"
    if sequence in ("\x00P", "\xe0P"):
        return "S-DOWN" if shift else "DOWN"
    ansi = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}
    if sequence.startswith("\x1b[") and sequence[-1:] in ansi:
        # CSI 1;2A denotes Shift+Up; plain ESC [ A is just Up.
        modifier = sequence[2:-1]
        shifted = shift or modifier.endswith(";2")
        return ("S-" if shifted else "") + ansi[sequence[-1]]
    if sequence == " ":
        return "SPACE"
    if sequence in ("\x03", "\x04"):
        return "QUIT"
    if len(sequence) == 1:
        return sequence.upper()
    return ""


def read_key():
    if os.name == "nt":
        import msvcrt
        import ctypes
        k = msvcrt.getwch()
        if k in ("\x00", "\xe0"):
            raw = k + msvcrt.getwch()
        elif k == "\x1b":
            # VS Code/Windows Terminal may emit VT input sequences.
            raw = k
            for _ in range(12):
                if not msvcrt.kbhit():
                    break
                raw += msvcrt.getwch()
                if raw[-1:] in "ABCD":
                    break
        else:
            raw = k
        shifted = bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
        return decode_key(raw, shifted)
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)  # Do not discard queued keystrokes.
        raw = os.read(fd, 1).decode("utf-8", "ignore")
        if raw == "\x1b":
            # Read only currently buffered bytes, never block on lone ESC.
            for _ in range(12):
                ready, _, _ = select.select([fd], [], [], .025)
                if not ready:
                    break
                raw += os.read(fd, 1).decode("utf-8", "ignore")
                if raw.startswith("\x1b[") and raw[-1:] in "ABCD":
                    break
        key = decode_key(raw)
        print(f"DEBUG: raw={raw!r}, key={key!r}", flush=True)
        return key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    

def main(argv=None):
    parser = argparse.ArgumentParser(description="ASCII GOLF v0.2")
    parser.add_argument("--debug", action="store_true", help="enable hidden development options")
    parser.add_argument("--seed", type=int, default=20261117, help="random seed for repeatable shots")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Run in an interactive terminal (VS Code terminal or Windows Terminal).")
        return 1
    min_width = W + 22  # ruler/headers beside the 59-column map
    min_height = H + 15
    size = shutil.get_terminal_size(fallback=(80, 40))
    if size.columns < min_width or size.lines < min_height:
        print(f"Tip: enlarge terminal to at least {min_width} columns x {min_height} rows ")
        print("for a complete HUD. The game remains playable in a smaller terminal.")
        print("=== SELECT DIFFICULTY ===")
    print("1. Easy")
    print("2. Normal")
    print("3. Hard")

    choice = input("Choose (1-3): ")

    difficulty = {
        "1": "Easy",
        "2": "Normal",
        "3": "Hard"
    }.get(choice, "Normal")

    game = Game(
        seed=args.seed,
        debug=args.debug,
        difficulty=difficulty
    )
    try:
        while True:
            print("\x1b[2J\x1b[H" + game.render(), flush=True)
            key = read_key()
            if key in ("Q", "QUIT"):
                break
            game.command(key)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        print("\x1b[0m\nThanks for playing ASCII GOLF!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
