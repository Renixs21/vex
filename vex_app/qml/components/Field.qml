import QtQuick
import QtQuick.Controls.Basic

// A text field in the Control Center's style: recessed, 44 px, a clear focus ring.
TextField {
    id: f
    property string label: ""
    implicitHeight: Theme.touch
    placeholderTextColor: Theme.label
    color: Theme.text
    font.family: Theme.sans
    font.pixelSize: 15
    leftPadding: 14
    rightPadding: 12
    selectByMouse: true
    verticalAlignment: TextInput.AlignVCenter
    Accessible.name: label || placeholderText
    background: Rectangle {
        radius: Theme.buttonRadius
        color: "#0f1113"
        border.width: f.activeFocus ? 2 : 1
        border.color: f.activeFocus ? Qt.rgba(0.9, 0.91, 0.93, 0.6) : Theme.outline
    }
}
