import QtQuick

// Tier pill: tinted background + 1px ring + light text, in the tier's colour (green / yellow / red).
Rectangle {
    id: root
    property string tier: "green"
    readonly property color c: Theme.tierColor(tier)
    implicitWidth: label.implicitWidth + 20
    implicitHeight: 24
    radius: height / 2
    color: Qt.rgba(c.r, c.g, c.b, 0.14)
    border.width: 1
    border.color: Qt.rgba(c.r, c.g, c.b, 0.55)
    Text {
        id: label
        anchors.centerIn: parent
        text: root.tier
        color: Qt.lighter(root.c, 1.18)
        font.family: Theme.sans
        font.pixelSize: 12
        font.weight: Font.Medium
        font.capitalization: Font.AllUppercase
        font.letterSpacing: 1.2
        renderType: Text.NativeRendering
    }
}
