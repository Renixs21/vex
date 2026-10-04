#version 440
// Brushed gunmetal panel: vertical gradient #1b1d21 -> #141619, very faint vertical brush lines, 1px outline #262a30,
// 1px top highlight rgba(255,255,255,0.07), rounded corners. One pass, anti-aliased at any size.
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    vec2 boxSize;        // item size in px
    float radius;     // corner radius in px
    vec4 topColor;         // gradient top colour
    vec4 bottomColor;      // gradient bottom colour
    vec4 outlineColor;     // 1px outline
    float highlight;  // top highlight alpha (0.07)
    float brush;      // brush line strength (0..1)
};

float hash1(float x){ return fract(sin(x*127.1) * 43758.5453); }

float roundedBox(vec2 p, vec2 b, float r){
    vec2 q = abs(p) - b + vec2(r);
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

void main(){
    vec2 px = qt_TexCoord0 * boxSize;
    vec2 c = px - boxSize*0.5;
    float d = roundedBox(c, boxSize*0.5, radius);
    float aa = 0.75;
    float fill = 1.0 - smoothstep(-aa, aa, d);
    if (fill <= 0.0) { fragColor = vec4(0.0); return; }

    vec3 col = mix(topColor.rgb, bottomColor.rgb, qt_TexCoord0.y);
    // brush: thin vertical streaks, slightly varying along y so it reads as metal, not stripes
    float x = floor(px.x);
    float streak = hash1(x) * 0.6 + hash1(floor(px.x*0.5) + 31.0) * 0.4;
    float along = 0.75 + 0.25*sin(px.y*0.013 + hash1(x)*6.28);
    col += (streak - 0.5) * 0.018 * brush * along;

    // 1px outline (inside the edge)
    float ring = 1.0 - smoothstep(0.0, 1.0, abs(d + 0.5) - 0.5);
    col = mix(col, outlineColor.rgb, ring * outlineColor.a);
    // 1px top highlight just inside the outline, fading out toward the corners
    float hl = (1.0 - smoothstep(0.0, 1.0, abs(px.y - 1.5) - 0.5)) * smoothstep(radius*0.6, radius*1.4, min(px.x, boxSize.x - px.x));
    col += vec3(1.0) * highlight * hl;

    fragColor = vec4(col * fill, fill) * qt_Opacity;
}
