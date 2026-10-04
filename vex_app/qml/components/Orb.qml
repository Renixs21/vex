import QtQuick

// The live orb (shaders/orb.frag): the edge orb's marbled liquid metal, breathing with Vex's state.
// vexState: idle | listening | thinking | speaking | followup | muted | asleep
ShaderEffect {
    id: orb
    property string vexState: "idle"
    property real time: 0
    property real flow: 0
    property real level: 0
    property real spin: 0
    property real listen: (vexState === "listening") ? 1 : 0
    property real think: (vexState === "thinking") ? 1 : 0
    property real speak: (vexState === "speaking") ? 1 : 0
    property real follow: (vexState === "followup") ? 1 : 0
    property real muted: (vexState === "muted" || vexState === "asleep") ? 1 : 0
    property real aspect: width / Math.max(1, height)
    Behavior on listen { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
    Behavior on think { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
    Behavior on speak { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
    Behavior on follow { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
    Behavior on muted { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
    fragmentShader: appPaths.shaders + "orb.frag.qsb"
    blending: true

    property real _env: 0
    property real _target: 0
    property real _next: 0
    FrameAnimation {
        running: orb.visible
        onTriggered: {
            const dt = Math.min(0.05, frameTime)
            orb.time += dt
            orb.flow += dt * (1.0 + 1.6 * orb.think + 0.8 * orb.speak + 0.4 * orb.listen) * (1.0 - 0.7 * orb.muted)
            orb.spin += dt * (0.05 + 0.35 * orb.think)
            // a gentle synthetic voice envelope while speaking (the app has no audio of its own)
            if (orb.speak > 0.01) {
                if (orb.time > orb._next) {
                    orb._target = Math.random() < 0.25 ? 0.1 : 0.35 + Math.random() * 0.65
                    orb._next = orb.time + 0.07 + Math.random() * 0.12
                }
            } else {
                orb._target = 0
            }
            orb._env += (orb._target - orb._env) * Math.min(1, dt * 14)
            orb.level = orb._env * orb.speak
        }
    }
}
