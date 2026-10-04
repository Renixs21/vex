import QtQuick

// A health-check verdict: PASS / WARN / FAIL / SKIP as a word (never colour alone) on a tinted pill.
Rectangle {
    id: root
    property string status: "PASS"
    readonly property color c: status === "PASS" ? Theme.statusDot : status === "WARN" ? Theme.yellow
                               : status === "FAIL" ? Theme.red : Theme.secondary
    implicitWidth: 58
    implicitHeight: 24
    radius: height / 2
    color: Qt.rgba(c.r, c.g, c.b, 0.14)
    border.width: 1
    border.color: Qt.rgba(c.r, c.g, c.b, 0.55)
    Text { anchors.centerIn: parent; text: root.status; color: Qt.lighter(root.c, 1.18); font.family: Theme.mono; font.pixelSize: 11
           font.weight: Font.Medium; renderType: Text.NativeRendering }
}
