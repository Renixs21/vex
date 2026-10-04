import QtQuick
import "../components"

// A screen that isn't built yet.
Item {
    property string title: ""
    Column {
        anchors.centerIn: parent
        spacing: 10
        SectionLabel { text: parent.parent.title; anchors.horizontalCenter: parent.horizontalCenter }
        SText { text: "This screen comes in a later step."; secondary: true; anchors.horizontalCenter: parent.horizontalCenter }
    }
}
