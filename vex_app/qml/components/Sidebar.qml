import QtQuick

// Left sidebar: mini orb + "VEX / Control Center", the nav, and a status card at the bottom. Collapses to icons (`compact`).
Item {
    id: root
    property bool compact: false
    property string current: "home"
    property string bottomTitle: ""
    property string bottomDetail: ""
    property real bottomFraction: -1
    signal navigate(string screen)
    width: compact ? 84 : 232
    Behavior on width { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }

    readonly property var items: [
        { key: "home", label: "Home" },
        { key: "conversations", label: "Conversations" },
        { key: "memory", label: "Memory" },
        { key: "tasks", label: "Tasks & reminders" },
        { key: "tools", label: "Tools & permissions" },
        { key: "voice", label: "Voice" },
        { key: "system", label: "System & health" },
        { key: "budget", label: "Budget" }
    ]

    Column {
        id: brand
        x: 16; y: 22
        spacing: 0
        Row {
            spacing: 12
            Orb { width: 40; height: 40; vexState: vex.connected ? vex.state : "muted" }
            Column {
                visible: !root.compact
                anchors.verticalCenter: parent.verticalCenter
                spacing: 2
                Text { text: "VEX"; color: Theme.text; font.family: Theme.sans; font.pixelSize: 16; font.weight: Font.DemiBold
                       font.letterSpacing: 3; renderType: Text.NativeRendering }
                SectionLabel { text: "Control Center"; font.pixelSize: 11 }
            }
        }
    }

    Column {
        id: nav
        anchors.top: brand.bottom
        anchors.topMargin: 28
        x: 12
        width: parent.width - 24
        spacing: 4
        Repeater {
            model: root.items
            delegate: Item {
                id: navItem
                required property var modelData
                required property int index
                width: nav.width
                height: Theme.touch
                activeFocusOnTab: true
                readonly property bool active: root.current === modelData.key
                Accessible.role: Accessible.PageTab
                Accessible.name: modelData.label
                Keys.onSpacePressed: root.navigate(modelData.key)
                Keys.onReturnPressed: root.navigate(modelData.key)
                Rectangle {
                    anchors.fill: parent
                    radius: Theme.buttonRadius
                    color: navItem.active ? "#1d2024" : (hover.containsMouse ? "#15171a" : "transparent")
                    border.width: navItem.active ? 1 : 0
                    border.color: Theme.outline
                    Behavior on color { ColorAnimation { duration: Theme.motion } }
                    Rectangle {                              // active marker
                        visible: navItem.active
                        width: 3; height: 18; radius: 1.5
                        x: 0; anchors.verticalCenter: parent.verticalCenter
                        color: Theme.text
                    }
                }
                Row {
                    x: root.compact ? (parent.width - 22) / 2 : 14
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 12
                    Image {
                        source: appPaths.icons + navItem.modelData.key + ".svg"
                        width: 22; height: 22; sourceSize: Qt.size(44, 44)
                        opacity: navItem.active ? 1 : 0.7
                    }
                    SText {
                        visible: !root.compact
                        text: navItem.modelData.label
                        color: navItem.active ? Theme.text : Theme.secondary
                        font.pixelSize: 14
                        font.weight: navItem.active ? Font.Medium : Font.Normal
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
                Rectangle {
                    anchors.fill: parent; anchors.margins: -2
                    radius: Theme.buttonRadius + 2
                    color: "transparent"; border.width: 2; border.color: Theme.focus
                    opacity: navItem.activeFocus ? 0.75 : 0
                }
                MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            onClicked: root.navigate(navItem.modelData.key) }
            }
        }
    }

    Panel {
        id: card
        visible: !root.compact
        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        anchors.margins: 12
        height: root.bottomFraction >= 0 ? 104 : 84
        padding: 16
        Column {
            anchors.fill: parent
            spacing: 6
            Row {
                spacing: 8
                Rectangle { width: 8; height: 8; radius: 4; anchors.verticalCenter: parent.verticalCenter
                            color: vex.connected ? Theme.statusDot : Theme.label }
                SText { text: root.bottomTitle; font.pixelSize: 14 }
            }
            SText { text: root.bottomDetail; mono: true; secondary: true; font.pixelSize: 13; width: parent.width }
            Rectangle {
                visible: root.bottomFraction >= 0
                width: parent.width; height: 6; radius: 3; color: "#0f1113"; border.width: 1; border.color: Theme.outline
                Rectangle { width: Math.max(6, parent.width * Math.max(0, root.bottomFraction)); height: parent.height; radius: 3
                            gradient: Gradient { orientation: Gradient.Horizontal
                                GradientStop { position: 0; color: Theme.chromeBottom } GradientStop { position: 1; color: Theme.chromeTop } } }
            }
        }
    }
    Rectangle {                                              // compact: just the status dot at the bottom
        visible: root.compact
        width: 10; height: 10; radius: 5
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom; anchors.bottomMargin: 28
        color: vex.connected ? Theme.statusDot : Theme.label
    }
}
