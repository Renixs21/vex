import QtQuick
import "../components"

// Vex isn't running: say so calmly, offer to start it, keep retrying in the background.
Item {
    id: root
    Column {
        anchors.centerIn: parent
        spacing: 22
        width: Math.min(460, parent.width - 64)
        Orb { width: 168; height: 168; vexState: "muted"; anchors.horizontalCenter: parent.horizontalCenter }
        Text {
            text: "Vex isn't running."
            color: Theme.text; font.family: Theme.sans; font.pixelSize: 30; font.weight: Font.Light
            anchors.horizontalCenter: parent.horizontalCenter
            renderType: Text.NativeRendering
        }
        SText {
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            secondary: true
            text: "Start it and this window connects by itself. It checks again every few seconds."
        }
        MetalButton {
            chrome: true
            text: "Start Vex"
            anchors.horizontalCenter: parent.horizontalCenter
            width: 180
            onClicked: vex.startVex()
            Component.onCompleted: forceActiveFocus()
        }
    }
}
