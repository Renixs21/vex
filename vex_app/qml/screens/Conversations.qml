import QtQuick
import QtQuick.Controls.Basic
import "../components"

// Conversations: sessions on the left (a wake-up and its follow-ups), the transcript of the chosen one on the right, with the tools each
// turn used and how long it took.
FocusScope {
    id: root
    Component.onCompleted: conversations.refresh()

    Panel {
        id: listPanel
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
        width: Math.min(400, root.width * 0.36)
        Item {
            anchors.fill: parent
            SectionLabel { id: head; text: "Sessions" }
            SText { anchors.top: head.bottom; anchors.topMargin: 14; visible: list.count === 0; secondary: true; width: parent.width
                    wrapMode: Text.WordWrap; elide: Text.ElideNone; font.pixelSize: 14
                    text: "No conversations yet. Talk to Vex and they show up here (kept for 14 days, secrets removed)." }
            ListView {
                id: list
                anchors { top: head.bottom; topMargin: 12; left: parent.left; right: parent.right; bottom: parent.bottom }
                clip: true
                spacing: 6
                model: conversations.sessions
                keyNavigationEnabled: true
                activeFocusOnTab: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                Keys.onReturnPressed: if (currentIndex >= 0) conversations.select(conversations.sessions[currentIndex].id)
                delegate: Item {
                    required property var modelData
                    required property int index
                    readonly property bool on: modelData.id === conversations.selected
                    width: list.width - 8
                    height: 84
                    Rectangle {
                        anchors.fill: parent
                        radius: Theme.buttonRadius
                        color: on ? "#1d2024" : (hover.containsMouse ? "#17191c" : "transparent")
                        border.width: on || (list.activeFocus && list.currentIndex === index) ? 1 : 0
                        border.color: list.activeFocus && list.currentIndex === index ? Qt.rgba(0.9, 0.91, 0.93, 0.6) : Theme.outline
                        Behavior on color { ColorAnimation { duration: Theme.motion } }
                    }
                    SText { x: 14; y: 12; mono: true; secondary: true; font.pixelSize: 12; text: modelData.when }
                    SText { x: 14; y: 12; width: parent.width - 28; horizontalAlignment: Text.AlignRight; mono: true; secondary: true
                            font.pixelSize: 12; text: modelData.turns + (modelData.turns === 1 ? " turn" : " turns")
                                                      + (modelData.latency ? "  ·  " + modelData.latency : "") }
                    SText { x: 14; y: 33; width: parent.width - 28; font.pixelSize: 15; text: modelData.first || "—" }
                    SText { x: 14; y: 56; width: parent.width - 28; mono: true; secondary: true; font.pixelSize: 12
                            text: modelData.tools.join("  ") }
                    MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: { list.currentIndex = index; conversations.select(modelData.id) } }
                }
            }
        }
    }

    Panel {
        id: transcript
        anchors { left: listPanel.right; leftMargin: 20; right: parent.right; top: parent.top; bottom: parent.bottom }
        Item {
            anchors.fill: parent
            Row {
                id: thead
                width: parent.width
                SectionLabel { text: "Transcript"; width: parent.width - when.width }
                SText { id: when; mono: true; secondary: true; font.pixelSize: 13; text: conversations.detail.when || "" }
            }
            ListView {
                id: turns
                anchors { top: thead.bottom; topMargin: 18; left: parent.left; right: parent.right; bottom: parent.bottom }
                clip: true
                spacing: 22
                model: conversations.detail.turns || []
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                delegate: Column {
                    required property var modelData
                    width: turns.width - 12
                    spacing: 10
                    // you
                    Item {
                        width: parent.width
                        height: youBubble.height
                        Rectangle {
                            id: youBubble
                            anchors.right: parent.right
                            width: Math.min(parent.width * 0.78, youText.implicitWidth + 32)
                            height: youText.implicitHeight + 22
                            radius: 14
                            color: "#202328"
                            border.width: 1; border.color: Theme.buttonOutline
                            SText { id: youText; x: 16; y: 11; width: Math.min(turns.width * 0.78 - 32, implicitWidth); text: modelData.said
                                    wrapMode: Text.WordWrap; elide: Text.ElideNone; font.pixelSize: 15 }
                        }
                    }
                    // tools used on the way
                    Flow {
                        visible: modelData.tools.length > 0
                        width: parent.width
                        spacing: 8
                        Repeater {
                            model: modelData.tools
                            delegate: Rectangle {
                                required property var modelData
                                height: 28; radius: 14
                                width: chip.implicitWidth + 30
                                color: "#15171a"; border.width: 1; border.color: Theme.outline
                                Row {
                                    id: chip
                                    anchors.centerIn: parent
                                    spacing: 8
                                    Rectangle { width: 7; height: 7; radius: 3.5; anchors.verticalCenter: parent.verticalCenter
                                                color: Theme.tierColor(modelData.tier) }
                                    SText { mono: true; font.pixelSize: 12; text: modelData.tool + (modelData.ok ? "" : " · " + modelData.decision)
                                            color: modelData.ok ? Theme.text : Theme.secondary }
                                }
                            }
                        }
                    }
                    // Vex
                    Row {
                        width: parent.width
                        spacing: 12
                        Orb { width: 26; height: 26; vexState: "idle" }
                        SText { width: parent.width - 40; text: modelData.reply; wrapMode: Text.WordWrap; elide: Text.ElideNone
                                font.pixelSize: 15; color: Theme.text }
                    }
                    SText { x: 38; width: parent.width - 38; wrapMode: Text.WordWrap; elide: Text.ElideNone; mono: true; secondary: true; font.pixelSize: 12
                            text: modelData.time + (modelData.via ? " · " + modelData.via : "") + (modelData.latency ? " · " + modelData.latency : "") }
                }
                SText { visible: turns.count === 0; anchors.centerIn: parent; secondary: true; text: "Pick a session." }
            }
        }
    }
}
