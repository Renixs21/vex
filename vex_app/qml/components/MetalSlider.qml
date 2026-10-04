import QtQuick
import QtQuick.Controls.Basic

// A machined slider: a recessed track, a chrome-filled part and a chrome knob. Arrow keys step it; 44 px tall.
Slider {
    id: s
    property string accessibleName: ""
    implicitHeight: Theme.touch
    Accessible.name: accessibleName
    background: Rectangle {
        x: s.leftPadding; y: s.topPadding + s.availableHeight / 2 - height / 2
        width: s.availableWidth; height: 8; radius: 4
        color: "#0f1113"; border.width: 1; border.color: Theme.outline
        Rectangle {
            width: s.visualPosition * parent.width; height: parent.height; radius: 4
            gradient: Gradient { orientation: Gradient.Horizontal
                GradientStop { position: 0; color: Theme.chromeBottom } GradientStop { position: 1; color: Theme.chromeTop } }
        }
    }
    handle: Rectangle {
        x: s.leftPadding + s.visualPosition * (s.availableWidth - width)
        y: s.topPadding + s.availableHeight / 2 - height / 2
        width: 24; height: 24; radius: 12
        border.width: 1; border.color: "#9aa1ab"
        gradient: Gradient {
            GradientStop { position: 0; color: s.pressed ? "#d9dde3" : Theme.chromeTop }
            GradientStop { position: 1; color: Theme.chromeBottom }
        }
        Rectangle { anchors.centerIn: parent; width: 32; height: 32; radius: 16; color: "transparent"; border.width: 2
                    border.color: Theme.focus; opacity: s.activeFocus ? 0.75 : 0 }
    }
}
