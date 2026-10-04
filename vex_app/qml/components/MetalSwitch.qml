import QtQuick

// A machined on/off switch: a recessed track and a chrome knob that slides (200 ms, ease-out). 44 px hit area, Space toggles.
Item {
    id: root
    property bool checked: false
    property bool busy: false
    property string accessibleName: ""
    signal toggled(bool on)
    implicitWidth: 56
    implicitHeight: Theme.touch
    activeFocusOnTab: true
    opacity: enabled ? 1 : 0.45
    Accessible.role: Accessible.CheckBox
    Accessible.name: accessibleName
    Accessible.checked: checked
    Keys.onSpacePressed: if (!busy) toggled(!checked)
    Keys.onReturnPressed: if (!busy) toggled(!checked)

    Rectangle {
        id: track
        width: 52; height: 28; radius: 14
        anchors.centerIn: parent
        border.width: 1
        border.color: root.checked ? "#4a5059" : Theme.buttonOutline
        gradient: Gradient {
            GradientStop { position: 0; color: root.checked ? "#3a3f47" : "#121417" }
            GradientStop { position: 1; color: root.checked ? "#2c3036" : "#181a1e" }
        }
        Behavior on border.color { ColorAnimation { duration: Theme.motion } }
        Rectangle {
            id: knob
            width: 22; height: 22; radius: 11
            y: 3
            x: root.checked ? track.width - width - 3 : 3
            Behavior on x { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
            border.width: 1
            border.color: "#9aa1ab"
            gradient: Gradient {
                GradientStop { position: 0; color: root.checked ? Theme.chromeTop : "#c5cad1" }
                GradientStop { position: 1; color: root.checked ? Theme.chromeBottom : "#8f959e" }
            }
            SequentialAnimation on opacity {
                running: root.busy; loops: Animation.Infinite
                NumberAnimation { to: 0.4; duration: 600; easing.type: Easing.InOutSine }
                NumberAnimation { to: 1; duration: 600; easing.type: Easing.InOutSine }
                onStopped: knob.opacity = 1
            }
        }
    }
    Rectangle {
        anchors.fill: track; anchors.margins: -3; radius: track.radius + 3
        color: "transparent"; border.width: 2; border.color: Theme.focus
        opacity: root.activeFocus ? 0.75 : 0
    }
    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: { root.forceActiveFocus(); if (!root.busy) root.toggled(!root.checked) }
    }
}
