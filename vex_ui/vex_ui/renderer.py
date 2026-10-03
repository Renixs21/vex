"""
Vex visual — GPU renderer.

Draws the whole scene (marbled, softly lit sphere with
slow liquid flow, glow, pop-in/fade and caption) in a single fragment shader.
It is window-agnostic: give it any moderngl context (a pygame window, or an
off-screen context for recording previews) and call render() every frame.
"""
from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass, field

import moderngl
import numpy as np
from PIL import Image, ImageDraw, ImageFont

VERT = """
#version 330
in vec2 in_pos;
uniform float u_flip;      // -1 renders upside-down so glReadPixels comes out top-row-first
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos.x, in_pos.y * u_flip, 0.0, 1.0);
}
"""

FRAG = """
#version 330
in vec2 v_uv;
out vec4 f_color;          // PREMULTIPLIED rgba

uniform vec2  u_res;
uniform float u_time;
uniform float u_flow;      // accumulated surface-flow time (faster when busy)
uniform float u_level;     // smoothed audio level 0..1
uniform float u_listen;    // 0..1 state weights (smoothly blended)
uniform float u_think;
uniform float u_speak;
uniform float u_wake;      // 0 -> 1 pop-in when waking
uniform float u_presence;  // 1 awake, 0 asleep
uniform float u_spin;      // slow swirl rotation (thinking)
uniform float u_overlay;   // 1 = transparent desktop overlay, 0 = window with dark background
uniform vec2  u_orb_c;     // orb centre (scene units, y in [-1,1])
uniform float u_orb_s;     // orb radius
uniform vec3  u_dark;      // palette
uniform vec3  u_mid;
uniform vec3  u_light;
uniform float u_text_a;
uniform float u_text_rise;
uniform vec4  u_text_rect;
uniform sampler2D u_text;

float hash(vec2 p){ p = fract(p*vec2(123.34, 456.21)); p += dot(p, p+45.32); return fract(p.x*p.y); }
float noise(vec2 p){
    vec2 i = floor(p), f = fract(p);
    vec2 u = f*f*(3.0-2.0*f);
    return mix(mix(hash(i), hash(i+vec2(1,0)), u.x), mix(hash(i+vec2(0,1)), hash(i+vec2(1,1)), u.x), u.y);
}
float fbm(vec2 p){
    float v = 0.0, a = 0.5;
    mat2 r = mat2(0.8, -0.6, 0.6, 0.8);
    for (int i = 0; i < 5; i++){ v += a*noise(p); p = r*p*2.03; a *= 0.5; }
    return v;
}
float fbm3(vec2 p){                       // smooth, low-detail version for the milky marble
    float v = 0.0, a = 0.55;
    mat2 r = mat2(0.8, -0.6, 0.6, 0.8);
    for (int i = 0; i < 3; i++){ v += a*noise(p); p = r*p*1.9; a *= 0.45; }
    return v / 0.9;
}
mat2 rot(float a){ float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }
vec4 over(vec4 dst, vec3 c, float a){ return vec4(c*a + dst.rgb*(1.0-a), a + dst.a*(1.0-a)); }
vec4 glow(vec4 dst, vec3 c, float g){ return vec4(dst.rgb + c*g, min(1.0, dst.a + g)); }

// marbled, softly lit sphere; q is the unit disc
vec3 marble(vec2 q, float distort, float bright){
    float z = sqrt(max(0.0, 1.0 - dot(q, q)));
    vec3 N = vec3(q, z);
    // wrap the 2D flow onto the sphere so it bends toward the edges
    vec2 uv = rot(u_spin) * (q / (0.5 + 0.5*z)) * 0.9;
    float t = u_flow;
    vec2 w1 = vec2(fbm3(uv*1.2 + vec2(t*0.10, -t*0.07)), fbm3(uv*1.2 + vec2(5.2 - t*0.06, 1.3 + t*0.09)));
    vec2 w2 = vec2(fbm3(uv*1.4 + w1*1.8 + vec2(1.7, 9.2) + t*0.05), fbm3(uv*1.4 + w1*1.8 + vec2(8.3, 2.8) - t*0.04));
    float m = fbm3(uv*1.3 + w2*(1.4 + 1.4*distort));

    // milky marble: mostly mid tone, soft darker veins and bright clouds
    vec3 c = mix(u_dark, u_mid, smoothstep(0.15, 0.60, m));
    c = mix(c, u_light, smoothstep(0.50, 0.95, m) * (0.40 + 0.35*bright));

    vec3 L = normalize(vec3(-0.50, -0.55, 0.67));        // light from lower-left, like the reference
    float diff = clamp(dot(N, L), 0.0, 1.0);
    c *= 0.72 + 0.40*diff;
    float hot = pow(max(dot(N, L), 0.0), 5.0);
    c = mix(c, u_light, hot * (0.55 + 0.35*bright));     // big soft hotspot
    vec3 V = normalize(vec3(0.55, 0.6, 0.6));
    c *= 1.0 - 0.22*pow(clamp(dot(N, V), 0.0, 1.0), 3.0) * (1.0 - z*0.5);   // shade upper-right
    float fres = pow(1.0 - z, 2.5);
    c = mix(c, u_mid * 1.2, fres * 0.30);                // gentle bright edge
    return c * (0.82 + 0.28*bright);
}

void main(){
    vec2 fc = v_uv * u_res;
    vec2 p = (2.0*fc - u_res) / u_res.y;
    float t = u_time;
    float lv = u_level;
    float ov = u_overlay;

    // pop-in on wake (ease-out-back), fade on sleep
    float k = clamp(u_wake, 0.0, 1.0);
    float pop = 1.0 + 2.2*pow(k - 1.0, 3.0) + 1.2*pow(k - 1.0, 2.0);
    float breathe = 1.0 + 0.018*sin(t*1.5) + 0.03*u_listen*sin(t*3.2) + 0.08*lv*u_speak + 0.05*lv*u_listen;
    float R = u_orb_s * breathe * max(pop, 0.0) * mix(0.85, 1.0, u_presence);
    float vis = u_presence * smoothstep(0.0, 0.25, k);

    vec2 op = p - u_orb_c;
    float ang = atan(op.y, op.x);
    float wob = (sin(ang*3.0 + t*5.0)*0.6 + sin(ang*5.0 - t*7.0)*0.4) * 0.012 * lv * u_speak;
    float d = length(op) - R*(1.0 + wob);
    float px = 2.0 / u_res.y;

    vec4 col = (ov > 0.5) ? vec4(0.0) : vec4(vec3(0.075, 0.078, 0.085), 1.0);
    if (ov < 0.5) col.rgb += u_mid * 0.05 * exp(-length(op) * 1.6);   // faint room glow

    // outer glow (stronger when listening / speaking, flash on wake)
    float gl = exp(-max(d, 0.0) / max(R, 0.01) * 4.0);
    float gStr = 0.10 + 0.18*u_listen + 0.10*u_think + 0.45*lv*u_speak + 0.6*k*(1.0-k);
    col = glow(col, u_mid, gl * gStr * vis);

    // sphere
    float inside = smoothstep(px, -px, d);
    if (inside > 0.0){
        vec2 q = op / max(R, 1e-3);
        float distort = 0.25 + 0.55*u_think + 1.1*lv*u_speak + 0.4*lv*u_listen;
        float bright = 0.15 + 0.45*u_listen + 0.35*u_think + 0.55*u_speak + 0.5*lv*u_speak;
        col = over(col, marble(clamp(q, -1.0, 1.0), distort, bright), inside * vis);
    }

    // caption (straight-alpha texture)
    vec2 tuv = (v_uv - u_text_rect.xy - vec2(0.0, -u_text_rise)) / u_text_rect.zw;
    if (all(greaterThanEqual(tuv, vec2(0.0))) && all(lessThanEqual(tuv, vec2(1.0)))){
        vec4 tx = texture(u_text, vec2(tuv.x, 1.0 - tuv.y));
        col = over(col, tx.rgb, tx.a * u_text_a * vis);
    }

    if (ov > 0.5){
        float e = smoothstep(0.0, 0.12, v_uv.x) * smoothstep(1.0, 0.88, v_uv.x)
                * smoothstep(1.0, 0.88, v_uv.y) * smoothstep(0.0, 0.06, v_uv.y);
        col *= e;
    } else {
        col.rgb += (hash(fc + t) - 0.5) * 0.008;
        col.a = 1.0;
    }
    col = clamp(col, 0.0, 1.0);
    col.rgb = min(col.rgb, vec3(col.a));
    f_color = col;
}
"""

STATES = ("idle", "listening", "thinking", "speaking", "sleep")

# Layout per mode: orb centre / radius in scene units, caption box (uv: y from bottom, height)
LAYOUT = {
    "overlay": {"orb_c": (0.0, 0.26), "orb_s": 0.36, "text_y": 0.08, "text_h": 0.26},
    "window":  {"orb_c": (0.0, 0.14), "orb_s": 0.24, "text_y": 0.14, "text_h": 0.16},
}

# (dark, mid, light) — same four looks as the reference
PALETTES = {
    "blue":    ((0.16, 0.29, 0.58), (0.36, 0.55, 0.86), (0.82, 0.93, 1.00)),
    "default": ((0.34, 0.34, 0.37), (0.62, 0.62, 0.65), (0.97, 0.97, 0.99)),
    "violet":  ((0.35, 0.21, 0.62), (0.62, 0.45, 0.88), (0.98, 0.86, 1.00)),
    "emerald": ((0.13, 0.40, 0.31), (0.32, 0.69, 0.55), (0.80, 1.00, 0.92)),
}


def _find_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        r"C:\Windows\Fonts\seguisb.ttf",     # Segoe UI Semibold
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


@dataclass
class OrbState:
    state: str = "idle"
    level: float = 0.0           # raw audio level you feed in (0..1)
    caption: str = ""
    palette: str = "blue"
    auto_speech: bool = True     # fake a speech envelope while "speaking" if no level arrives
    w: dict = field(default_factory=lambda: {s: 0.0 for s in STATES})
    smooth_level: float = 0.0
    wake: float = 1.0
    presence: float = 1.0
    flow: float = 0.0
    spin: float = 0.0
    text_a: float = 0.0
    text_rise: float = 0.0
    last_level_at: float = -99.0


class VexRenderer:
    """
    mode="overlay": transparent background (premultiplied alpha) — orb floats on the desktop.
    mode="window":  opaque, dark background (widescreen / fullscreen).
    flip=True renders upside-down so fbo.read() returns rows top-first (ready for Qt/PIL).
    """
    def __init__(self, ctx: moderngl.Context, size: tuple[int, int], mode: str = "overlay", flip: bool = True):
        self.ctx = ctx
        self.size = size
        self.mode = mode
        self.flip = flip
        self.prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        self.vbo = ctx.buffer(quad.tobytes())
        self.vao = ctx.vertex_array(self.prog, [(self.vbo, "2f", "in_pos")])
        self.s = OrbState()
        self.s.w["idle"] = 1.0
        self._text_tex = None
        self._text_for = None
        self._rng = random.Random(7)
        self._env = 0.0
        self._env_target = 0.0
        self._env_next = 0.0

    # ---------- public control ----------
    def set_state(self, state: str):
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        if state != "sleep" and self.s.state == "sleep":
            self.s.presence = max(self.s.presence, 0.0)
        self.s.state = state

    def set_level(self, level: float, now: float):
        self.s.level = max(0.0, min(1.0, float(level)))
        self.s.last_level_at = now

    def set_caption(self, text: str):
        if text != self.s.caption:
            self.s.caption = text
            self.s.text_a = 0.0
            self.s.text_rise = 0.03

    def wake(self):
        """Pop the orb in (used when 'Hey Vex' is heard)."""
        if self.s.state == "sleep":
            self.s.state = "idle"
        self.s.presence = 1.0
        self.s.wake = 0.0

    def set_palette(self, name: str):
        if name not in PALETTES:
            raise ValueError(f"palette must be one of {tuple(PALETTES)}")
        self.s.palette = name

    def resize(self, size):
        self.size = size
        self._text_for = None

    @property
    def asleep(self) -> bool:
        return self.s.state == "sleep" and self.s.presence < 0.01 and self.s.wake >= 1.0

    # ---------- caption texture ----------
    def _caption_texture(self):
        key = (self.s.caption, self.size, self.mode)
        if self._text_for == key:
            return
        W, H = self.size
        lay = LAYOUT[self.mode]
        box_w, box_h = int(min(W * 0.9, H * 1.25)), int(H * lay["text_h"])
        img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
        if self.s.caption:
            size = max(14, int(H * (0.050 if self.mode == "overlay" else 0.046)))
            font = _find_font(size)
            draw = ImageDraw.Draw(img)
            words, lines, cur = self.s.caption.split(), [], ""
            for wd in words:
                trial = (cur + " " + wd).strip()
                if draw.textlength(trial, font=font) <= box_w * 0.86:
                    cur = trial
                else:
                    lines.append(cur); cur = wd
            if cur:
                lines.append(cur)
            lines = lines[:3]
            lh = int(size * 1.25)
            y0 = (box_h - lh * len(lines)) // 2
            if self.mode == "overlay":
                # soft dark shadow so the text reads on any wallpaper
                from PIL import ImageFilter
                sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
                sd = ImageDraw.Draw(sh)
                y = y0
                for ln in lines:
                    tw = draw.textlength(ln, font=font)
                    sd.text(((box_w - tw) / 2, y + size * 0.06), ln, font=font, fill=(4, 8, 40, 235))
                    y += lh
                sh = sh.filter(ImageFilter.GaussianBlur(size * 0.28))
                img = Image.alpha_composite(img, sh)
                draw = ImageDraw.Draw(img)
            y = y0
            for ln in lines:
                tw = draw.textlength(ln, font=font)
                draw.text(((box_w - tw) / 2, y), ln, font=font, fill=(246, 248, 255, 255))
                y += lh
        if self._text_tex:
            self._text_tex.release()
        self._text_tex = self.ctx.texture(img.size, 4, img.tobytes())
        self._text_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._text_rect = ((1 - box_w / W) / 2, lay["text_y"], box_w / W, box_h / H)
        self._text_for = key

    # ---------- per-frame ----------
    def _fake_speech(self, t, dt):
        if t >= self._env_next:
            if self._rng.random() < 0.18:
                self._env_target = 0.0
                self._env_next = t + self._rng.uniform(0.15, 0.35)
            else:
                self._env_target = self._rng.uniform(0.35, 1.0)
                self._env_next = t + self._rng.uniform(0.07, 0.16)
        self._env += (self._env_target - self._env) * min(1.0, dt * 22.0)
        return self._env

    def update(self, t: float, dt: float):
        s = self.s
        k = min(1.0, dt * 4.0)
        for name in STATES:
            target = 1.0 if s.state == name else 0.0
            s.w[name] += (target - s.w[name]) * k

        target_p = 0.0 if s.state == "sleep" else 1.0
        s.presence += (target_p - s.presence) * min(1.0, dt * (2.2 if target_p < s.presence else 6.0))

        level = s.level
        if s.state == "speaking" and s.auto_speech and (t - s.last_level_at) > 0.5:
            level = self._fake_speech(t, dt)
        elif s.state != "speaking" and (t - s.last_level_at) > 0.5:
            level = 0.0
        rate = 18.0 if level > s.smooth_level else 7.0
        s.smooth_level += (level - s.smooth_level) * min(1.0, dt * rate)

        w = s.w
        s.flow += dt * (0.5 + 0.6 * w["listening"] + 1.6 * w["thinking"] + 1.0 * w["speaking"]
                        + 2.5 * s.smooth_level * w["speaking"])
        s.spin += dt * 0.35 * w["thinking"]
        if s.wake < 1.0:
            s.wake = min(1.0, s.wake + dt / 0.9)
        if s.caption:
            s.text_a += (1.0 - s.text_a) * min(1.0, dt * 3.0)
            s.text_rise += (0.0 - s.text_rise) * min(1.0, dt * 4.0)

    def render(self, t: float, dt: float):
        self.update(t, dt)
        self._caption_texture()
        s, p = self.s, self.prog
        lay = LAYOUT[self.mode]
        p["u_flip"].value = -1.0 if self.flip else 1.0
        p["u_res"].value = tuple(float(v) for v in self.size)
        p["u_time"].value = t
        p["u_flow"].value = s.flow
        p["u_level"].value = s.smooth_level
        p["u_listen"].value = s.w["listening"]
        p["u_think"].value = s.w["thinking"]
        p["u_speak"].value = s.w["speaking"]
        p["u_wake"].value = s.wake
        p["u_presence"].value = s.presence
        p["u_spin"].value = s.spin
        p["u_overlay"].value = 1.0 if self.mode == "overlay" else 0.0
        p["u_orb_c"].value = lay["orb_c"]
        p["u_orb_s"].value = lay["orb_s"]
        dk, md, lt = PALETTES[s.palette]
        p["u_dark"].value, p["u_mid"].value, p["u_light"].value = dk, md, lt
        p["u_text_a"].value = s.text_a if s.caption else 0.0
        p["u_text_rise"].value = s.text_rise
        p["u_text_rect"].value = self._text_rect
        self._text_tex.use(0)
        p["u_text"].value = 0
        self.ctx.viewport = (0, 0, *self.size)
        self.vao.render(moderngl.TRIANGLE_STRIP)
