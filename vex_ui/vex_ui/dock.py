"""
Vex visual — edge-dock renderer.

One shape that morphs between a thin pill parked on a screen edge (asleep) and the marbled
orb in the middle of the screen (awake). A single spring drives position and shape together,
and the marble swirl clock never resets, so the colours flow through the whole transition.

Geometry is in *logical* pixels with a top-left origin (what Qt uses); the GPU works in
physical pixels, so everything is multiplied by `dpr` when it reaches the shader.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import moderngl
import numpy as np

from vex_ui.renderer import PALETTES

FRAG = r"""
#version 330
out vec4 f_color;                      // premultiplied rgba
uniform float u_px;                    // device pixel ratio
uniform float u_flow, u_bright, u_alpha, u_glow, u_distort, u_morph;
uniform vec2  u_pos, u_half;           // physical px, GL origin (bottom-left)
uniform vec3  u_dark, u_mid, u_light;

float hash(vec2 p){ p=fract(p*vec2(123.34,456.21)); p+=dot(p,p+45.32); return fract(p.x*p.y); }
float noise(vec2 p){ vec2 i=floor(p),f=fract(p); vec2 u=f*f*(3.-2.*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),u.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),u.x),u.y); }
float fbm3(vec2 p){ float v=0.,a=.55; mat2 r=mat2(.8,-.6,.6,.8);
  for(int i=0;i<3;i++){v+=a*noise(p);p=r*p*1.9;a*=.45;} return v/.9; }

// rounded box whose corner radius is its smaller half-size: a capsule when thin, a circle when square
float sd(vec2 p){ vec2 h=u_half; float r=min(h.x,h.y); vec2 q=abs(p-u_pos)-(h-r);
  return length(max(q,0.))+min(max(q.x,q.y),0.)-r; }

void main(){
  vec2 p=gl_FragCoord.xy;
  float d=sd(p);
  float r=min(u_half.x,u_half.y);
  float g=exp(-max(d,0.)/(mix(14.,34.,u_morph)*u_px))*u_glow;
  vec4 col=vec4(u_mid*g,g);
  float cov=smoothstep(1.,-1.,d);
  if(cov>0.){
    vec2 e=vec2(1.,0.);
    vec2 gr=vec2(sd(p+e.xy)-sd(p-e.xy), sd(p+e.yx)-sd(p-e.yx));
    gr=length(gr)>1e-4?normalize(gr):vec2(0.);
    float t=clamp(-d/r,0.,1.);
    float z=sqrt(max(0.,1.-(1.-t)*(1.-t)));
    vec3 N=normalize(vec3(gr*(1.-t),max(z,0.05)));
    // marble coordinates follow the shape, so the swirl stretches as the pill fills out
    vec2 uv=((p-u_pos)/u_half)*0.9; float T=u_flow;
    vec2 w1=vec2(fbm3(uv*1.2+vec2(T*.10,-T*.07)),fbm3(uv*1.2+vec2(5.2-T*.06,1.3+T*.09)));
    vec2 w2=vec2(fbm3(uv*1.4+w1*1.8+vec2(1.7,9.2)+T*.05),fbm3(uv*1.4+w1*1.8+vec2(8.3,2.8)-T*.04));
    float m=fbm3(uv*1.3+w2*(1.4+1.4*u_distort));
    vec3 c=mix(u_dark,u_mid,smoothstep(.15,.60,m));
    c=mix(c,u_light,smoothstep(.50,.95,m)*(.40+.35*u_bright));
    vec3 L=normalize(vec3(-.5,-.55,.67));
    float diff=clamp(dot(N,L),0.,1.);
    c*=.72+.40*diff;
    c=mix(c,u_light,pow(max(dot(N,L),0.),5.)*(.55+.35*u_bright));
    c=mix(c,u_mid*1.2,pow(1.-z,2.5)*.30);
    c*=.82+.28*u_bright;
    float a=cov*u_alpha;
    col=vec4(c*a+col.rgb*(1.-a),a+col.a*(1.-a));
  }
  col=clamp(col,0.,1.);
  col.rgb=min(col.rgb,vec3(col.a));
  f_color=col;
}
"""

STATES = ("idle", "listening", "thinking", "speaking", "sleep")
#            bright glow  flow distort alpha
LOOK = {
    "sleep":     (0.30, 0.10, 0.35, 0.2, 0.85),
    "idle":      (0.40, 0.18, 0.55, 0.3, 1.0),
    "listening": (0.65, 0.30, 0.90, 0.5, 1.0),
    "thinking":  (0.55, 0.22, 2.20, 1.0, 1.0),
    "speaking":  (0.90, 0.30, 1.60, 1.0, 1.0),
}
KEYS = ("bright", "glow", "rate", "dist", "alpha")


def _smooth(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


@dataclass
class DockConfig:
    edge: str = "right"          # "right" | "left"
    edge_y: float = 0.40         # pill height on the edge, fraction of screen height from the top
    center_y: float = 0.50       # where the orb lands, fraction of screen height from the top
    orb_radius: float = 62.0     # logical px
    pill_half: tuple = (7.0, 58.0)   # logical half width / half height of the parked pill
    speed: float = 1.0           # 1.0 = ~1 s move; 0.7 slower, 1.3 faster
    margin: float = 130.0        # glow margin around the shape that is redrawn each frame


@dataclass
class DockState:
    state: str = "sleep"
    palette: str = "blue"
    caption: str = ""
    level: float = 0.0
    s: float = 0.0               # 0 = parked pill, 1 = orb in the middle (springs, can overshoot)
    v: float = 0.0
    target: float = 0.0
    flow: float = 0.0
    hover: float = 0.0
    hover_target: float = 0.0
    smooth_level: float = 0.0
    text_a: float = 0.0
    text_rise: float = 0.0
    last_level_at: float = -99.0
    look: dict = field(default_factory=lambda: dict(zip(KEYS, LOOK["sleep"])))


class DockRenderer:
    def __init__(self, ctx: moderngl.Context, win_size: tuple[int, int], dpr: float = 1.0,
                 cfg: DockConfig | None = None):
        self.ctx, self.dpr = ctx, float(dpr)
        self.cfg = cfg or DockConfig()
        self.W, self.H = win_size                       # logical
        self.pw, self.ph = int(round(self.W * dpr)), int(round(self.H * dpr))
        self.prog = ctx.program(
            vertex_shader="#version 330\nin vec2 p;void main(){gl_Position=vec4(p,0,1);}",
            fragment_shader=FRAG)
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        self.vao = ctx.vertex_array(self.prog, [(ctx.buffer(quad.tobytes()), "2f", "p")])
        self.fbo = ctx.simple_framebuffer((self.pw, self.ph), components=4)
        self.s = DockState()
        self.geo = (0.0, 0.0, 1.0, 1.0)                 # cx, cy, hx, hy  (logical, top-left origin)
        self.morph = 0.0
        self._prev_box = None
        self._rng = random.Random(7)
        self._env = self._env_t = self._env_next = 0.0
        self._compute_geometry()

    # ------------------------------------------------------------ control
    def set_state(self, state: str):
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        self.s.state = state
        self.s.target = 0.0 if state == "sleep" else 1.0

    def wake(self):
        if self.s.state == "sleep":
            self.s.state = "idle"
        self.s.target = 1.0

    def sleep(self):
        self.set_state("sleep")

    @property
    def docked(self) -> bool:
        return self.s.target < 0.5

    def set_level(self, level: float, now: float):
        self.s.level = max(0.0, min(1.0, float(level)))
        self.s.last_level_at = now

    def set_caption(self, text: str):
        if text != self.s.caption:
            self.s.caption = text
            self.s.text_a = 0.0
            self.s.text_rise = 6.0

    def set_palette(self, name: str):
        if name not in PALETTES:
            raise ValueError(f"palette must be one of {tuple(PALETTES)}")
        self.s.palette = name

    def set_hover(self, on: bool):
        self.s.hover_target = 1.0 if on else 0.0

    # ------------------------------------------------------------ geometry
    def _compute_geometry(self):
        c, s_, W, H = self.cfg, self.s, self.W, self.H
        phx, phy = c.pill_half
        park_x = W - (phx + 4) if c.edge == "right" else phx + 4
        park = (park_x, H * c.edge_y)
        center = (W / 2, H * c.center_y)
        s = s_.s
        cx = park[0] + (center[0] - park[0]) * s
        cy = park[1] + (center[1] - park[1]) * s
        self.morph = _smooth(s / 0.9)
        m = self.morph
        hx = phx + (c.orb_radius - phx) * m
        hy = phy + (c.orb_radius - phy) * m
        hx += 2.5 * s_.hover * (1 - m)                        # pill widens a little under the cursor
        sp = abs(s_.v) * _smooth(m * 1.4)                     # stretch along the motion while moving
        f = 1 + min(0.10 * sp, 0.14)
        hx *= f
        hy /= math.sqrt(f)
        lv = s_.smooth_level if s_.state == "speaking" else 0.0
        br = 1 + 0.03 * math.sin(self._t * 1.5) * m + 0.07 * lv * m if hasattr(self, "_t") else 1.0
        self.geo = (cx, cy, max(hx * br, 1.0), max(hy * br, 1.0))

    def caption_rect(self):
        cx, cy, _, hy = self.geo
        w = min(self.W * 0.6, 560.0)
        return (cx - w / 2, cy + hy + 16 - self.s.text_rise, w, 96.0)

    def caption_alpha(self) -> float:
        return self.s.text_a * _smooth((self.morph - 0.7) / 0.3) if self.s.caption else 0.0

    def hit_rect(self):
        """Region that should receive the mouse (logical): the pill, or the ball."""
        cx, cy, hx, hy = self.geo
        pad = 12 if self.morph < 0.5 else 4
        return (cx - hx - pad, cy - hy - pad, 2 * (hx + pad), 2 * (hy + pad)), self.morph >= 0.5

    # ------------------------------------------------------------ per frame
    def _fake_speech(self, t, dt):
        if t >= self._env_next:
            if self._rng.random() < 0.18:
                self._env_t, self._env_next = 0.0, t + self._rng.uniform(0.15, 0.35)
            else:
                self._env_t, self._env_next = self._rng.uniform(0.35, 1.0), t + self._rng.uniform(0.07, 0.16)
        self._env += (self._env_t - self._env) * min(1.0, dt * 22.0)
        return self._env

    def update(self, t: float, dt: float):
        self._t = t
        s, c = self.s, self.cfg
        K, C = 10.0 * c.speed ** 2, 4.7 * c.speed                       # soft spring: ~1 s move, ~3% overshoot
        for _ in range(4):
            h = dt / 4
            s.v += (K * (s.target - s.s) - C * s.v) * h
            s.s += s.v * h

        tgt = dict(zip(KEYS, LOOK[s.state]))
        level = s.level
        if s.state == "speaking" and (t - s.last_level_at) > 0.5:
            level = self._fake_speech(t, dt)
        elif s.state != "speaking" and (t - s.last_level_at) > 0.5:
            level = 0.0
        s.smooth_level += (level - s.smooth_level) * min(1.0, dt * (18.0 if level > s.smooth_level else 7.0))
        lv = s.smooth_level
        if s.state == "speaking":
            tgt["glow"] += 0.45 * lv; tgt["rate"] += 2.2 * lv; tgt["dist"] += 1.0 * lv; tgt["bright"] += 0.4 * lv
        elif s.state == "listening":
            tgt["glow"] += 0.30 * lv; tgt["bright"] += 0.25 * lv
        k = min(1.0, dt * (14 if s.state == "speaking" else 5))
        for key in KEYS:
            s.look[key] += (tgt[key] - s.look[key]) * k
        s.flow += dt * s.look["rate"]                                  # never reset: colours keep flowing
        s.hover += (s.hover_target - s.hover) * min(1.0, dt * 10)
        if s.caption:
            s.text_a += (1.0 - s.text_a) * min(1.0, dt * 3.0)
            s.text_rise += (0.0 - s.text_rise) * min(1.0, dt * 4.0)
        self._compute_geometry()

    def _box(self):
        """Logical bounding box that must be redrawn this frame (shape + glow [+ caption])."""
        cx, cy, hx, hy = self.geo
        m = self.cfg.margin
        x0, y0, x1, y1 = cx - hx - m, cy - hy - m, cx + hx + m, cy + hy + m
        if self.caption_alpha() > 0.003 or (self.s.caption and self.morph > 0.3):
            rx, ry, rw, rh = self.caption_rect()
            x0, y0, x1, y1 = min(x0, rx - 20), min(y0, ry - 10), max(x1, rx + rw + 20), max(y1, ry + rh + 24)
        return [x0, y0, x1, y1]

    def render(self):
        """Draw the frame. Returns (rgba_bytes_bottom_row_first, (x, y, w, h) in physical px, top-left origin)."""
        box = self._box()
        if self._prev_box:                                  # also wipe where the shape was last frame
            p = self._prev_box
            box = [min(box[0], p[0]), min(box[1], p[1]), max(box[2], p[2]), max(box[3], p[3])]
        self._prev_box = self._box()
        d = self.dpr
        x0 = max(0, int(math.floor(box[0] * d))); y0 = max(0, int(math.floor(box[1] * d)))
        x1 = min(self.pw, int(math.ceil(box[2] * d))); y1 = min(self.ph, int(math.ceil(box[3] * d)))
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        gl_y = self.ph - y1

        s, p = self.s, self.prog
        cx, cy, hx, hy = self.geo
        dk, md, lt = PALETTES[s.palette]
        pulse = 0.07 * math.sin(self._t * 1.7) if self.morph < 0.3 else 0.0     # shimmer while parked
        hov = s.hover * (1 - self.morph)
        p["u_px"].value = d
        p["u_flow"].value = s.flow
        p["u_bright"].value = s.look["bright"] + pulse + 0.25 * hov
        p["u_alpha"].value = min(1.0, s.look["alpha"] + 0.1 * hov)
        p["u_glow"].value = s.look["glow"] + 0.5 * pulse + 0.15 * hov
        p["u_distort"].value = s.look["dist"]
        p["u_morph"].value = self.morph
        p["u_pos"].value = (cx * d, (self.H - cy) * d)
        p["u_half"].value = (hx * d, hy * d)
        p["u_dark"].value, p["u_mid"].value, p["u_light"].value = dk, md, lt

        self.fbo.use()
        self.fbo.scissor = (x0, gl_y, w, h)
        self.fbo.clear(0.0, 0.0, 0.0, 0.0, viewport=(x0, gl_y, w, h))
        self.vao.render(moderngl.TRIANGLE_STRIP)
        data = self.fbo.read(viewport=(x0, gl_y, w, h), components=4)
        self.fbo.scissor = None
        return data, (x0, y0, w, h)
