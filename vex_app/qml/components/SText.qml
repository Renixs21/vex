import QtQuick

// Body text in Geist (set mono: true for numbers, times and tool names: Geist Mono).
Text {
    property bool mono: false
    property bool secondary: false
    color: secondary ? Theme.secondary : Theme.text
    font.family: mono ? Theme.mono : Theme.sans
    font.pixelSize: 15
    elide: Text.ElideRight
    renderType: Text.NativeRendering
}
