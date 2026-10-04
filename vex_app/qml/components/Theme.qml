pragma Singleton
import QtQuick

// The Control Center's look, in one place (docs/APP.md). No colour except the orb, the status dot and the tier pills.
QtObject {
    readonly property color base: "#0b0c0e"
    readonly property color panelTop: "#1b1d21"
    readonly property color panelBottom: "#141619"
    readonly property color outline: "#262a30"
    readonly property color text: "#e6e9ed"
    readonly property color secondary: "#a0a6af"
    readonly property color label: "#8d939c"
    readonly property color buttonTop: "#2a2e34"
    readonly property color buttonBottom: "#1e2126"
    readonly property color buttonOutline: "#33373e"
    readonly property color chromeTop: "#f1f3f6"
    readonly property color chromeBottom: "#b8bec7"
    readonly property color chromeText: "#0b0c0e"
    readonly property color focus: "#e6e9ed"
    readonly property color statusDot: "#6fcf97"
    readonly property color green: "#6fcf97"
    readonly property color yellow: "#e2c26b"
    readonly property color red: "#e2776b"
    readonly property color edge: "#3a3e45"

    readonly property string sans: "Geist"
    readonly property string mono: "Geist Mono"
    readonly property int radius: 16
    readonly property int buttonRadius: 10
    readonly property int touch: 44                      // minimum touch target
    readonly property int motion: 200                    // 180-240 ms, ease-out, no bounce
    readonly property int easing: Easing.OutCubic
    readonly property real labelTracking: 13 * 0.14       // 0.14em at 13 px

    function tierColor(tier) {
        return tier === "green" ? green : tier === "yellow" ? yellow : tier === "red" ? red : secondary
    }
}
