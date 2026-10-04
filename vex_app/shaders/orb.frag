#version 440
// The Control Center's orb: the same marbled, softly lit sphere as vex_ui/vex_ui/renderer.py (the edge orb), as a Qt ShaderEffect.
// Compile: python tools_dev/build_shaders.py  (qsb -> orb.frag.qsb, with HLSL/GLSL/MSL so every Qt backend can run it)
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float time;      // seconds
    float flow;      // accumulated surface-flow time (faster when busy)
    float level;     // 0..1 voice level (a gentle synthetic envelope while speaking)
    float listen;    // 0..1 state weights, blended in QML
    float think;
    float speak;
    float follow;    // follow-up window: listening, but calmer
    float muted;     // 0..1: grey, slow, dim
    float spin;      // slow swirl rotation
    float aspect;    // width / height of the item
};

float hash(vec2 p){ p = fract(p*vec2(123.34, 456.21)); p += dot(p, p+45.32); return fract(p.x*p.y); }
float noise(vec2 p){
    vec2 i = floor(p), f = fract(p);
    vec2 u = f*f*(3.0-2.0*f);
    return mix(mix(hash(i), hash(i+vec2(1,0)), u.x), mix(hash(i+vec2(0,1)), hash(i+vec2(1,1)), u.x), u.y);
}
float fbm3(vec2 p){
    float v = 0.0, a = 0.55;
    mat2 r = mat2(0.8, -0.6, 0.6, 0.8);
    for (int i = 0; i < 3; i++){ v += a*noise(p); p = r*p*1.9; a *= 0.45; }
    return v / 0.9;
}
mat2 rot(float a){ float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }

// palette "blue" from the edge orb (dark, mid, light)
const vec3 DARK  = vec3(0.16, 0.29, 0.58);
const vec3 MID   = vec3(0.36, 0.55, 0.86);
const vec3 LIGHT = vec3(0.82, 0.93, 1.00);
const vec3 GREY_D = vec3(0.20, 0.21, 0.23);
const vec3 GREY_M = vec3(0.40, 0.41, 0.44);
const vec3 GREY_L = vec3(0.70, 0.71, 0.74);

vec3 marble(vec2 q, float distort, float bright, vec3 dk, vec3 md, vec3 lt){
    float z = sqrt(max(0.0, 1.0 - dot(q, q)));
    vec3 N = vec3(q, z);
    vec2 uv = rot(spin) * (q / (0.5 + 0.5*z)) * 0.9;
    float t = flow;
    vec2 w1 = vec2(fbm3(uv*1.2 + vec2(t*0.10, -t*0.07)), fbm3(uv*1.2 + vec2(5.2 - t*0.06, 1.3 + t*0.09)));
    vec2 w2 = vec2(fbm3(uv*1.4 + w1*1.8 + vec2(1.7, 9.2) + t*0.05), fbm3(uv*1.4 + w1*1.8 + vec2(8.3, 2.8) - t*0.04));
    float m = fbm3(uv*1.3 + w2*(1.4 + 1.4*distort));
    vec3 c = mix(dk, md, smoothstep(0.15, 0.60, m));
    c = mix(c, lt, smoothstep(0.50, 0.95, m) * (0.40 + 0.35*bright));
    vec3 L = normalize(vec3(-0.50, 0.55, 0.67));             // light from the upper-left (y points up here)
    float diff = clamp(dot(N, L), 0.0, 1.0);
    c *= 0.72 + 0.40*diff;
    float hot = pow(max(dot(N, L), 0.0), 5.0);
    c = mix(c, lt, hot * (0.55 + 0.35*bright));
    vec3 V = normalize(vec3(0.55, -0.6, 0.6));
    c *= 1.0 - 0.22*pow(clamp(dot(N, V), 0.0, 1.0), 3.0) * (1.0 - z*0.5);
    float fres = pow(1.0 - z, 2.5);
    c = mix(c, md * 1.2, fres * 0.30);
    return c * (0.82 + 0.28*bright);
}

void main(){
    vec2 p = qt_TexCoord0 * 2.0 - 1.0;
    p.y = -p.y;
    p.x *= aspect;
    float lv = level;
    float act = clamp(listen + follow*0.6, 0.0, 1.0);
    float breathe = 1.0 + 0.018*sin(time*1.5) + 0.03*act*sin(time*3.2) + 0.08*lv*speak + (1.0 - muted)*0.0;
    float R = 0.62 * breathe;
    float ang = atan(p.y, p.x);
    float wob = (sin(ang*3.0 + time*5.0)*0.6 + sin(ang*5.0 - time*7.0)*0.4) * 0.012 * lv * speak;
    float d = length(p) - R*(1.0 + wob);
    float px = 2.5 / 600.0;

    vec3 dk = mix(DARK, GREY_D, muted), md = mix(MID, GREY_M, muted), lt = mix(LIGHT, GREY_L, muted);
    vec4 col = vec4(0.0);
    // outer glow: stronger when listening / speaking
    float gl = exp(-max(d, 0.0) / R * 4.0);
    float gStr = (0.10 + 0.18*act + 0.10*think + 0.45*lv*speak) * (1.0 - 0.7*muted);
    col.rgb += md * gl * gStr;
    col.a = clamp(gl * gStr, 0.0, 1.0);

    float inside = smoothstep(px, -px, d);
    if (inside > 0.0){
        vec2 q = p / R;
        float distort = 0.25 + 0.55*think + 1.1*lv*speak + 0.4*lv*act;
        float bright = (0.15 + 0.45*act + 0.35*think + 0.55*speak + 0.5*lv*speak) * (1.0 - 0.6*muted);
        vec3 c = marble(clamp(q, -1.0, 1.0), distort, bright, dk, md, lt);
        col.rgb = c*inside + col.rgb*(1.0 - inside);
        col.a = inside + col.a*(1.0 - inside);
    }
    // fade everything to nothing well inside the item's edges, so the glow never shows the quad's square outline
    float edge = max(abs(p.x) / max(aspect, 1e-3), abs(p.y));
    col *= 1.0 - smoothstep(0.80, 0.98, edge);
    col.rgb = min(col.rgb, vec3(col.a));                      // premultiplied
    fragColor = col * qt_Opacity;
}
