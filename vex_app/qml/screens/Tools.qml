import QtQuick
import QtQuick.Controls.Basic
import "../components"

// Tools & permissions: every tool (tier, what it does, last used, on/off), the audit log with Undo/Restore, and how tiers work.
FocusScope {
    id: root
    Component.onCompleted: tools.refresh()
    readonly property var tierKeys: ["all", "green", "yellow", "red"]

    SegmentedTabs {
        id: tierTabs
        anchors { left: parent.left; top: parent.top }
        tabs: ["All  " + (tools.counts.all || 0), "Green  " + (tools.counts.green || 0), "Yellow  " + (tools.counts.yellow || 0),
               "Red  " + (tools.counts.red || 0)]
        current: Math.max(0, root.tierKeys.indexOf(tools.tier))
        onPicked: (i) => tools.setTier(root.tierKeys[i])
    }
    TextField {
        id: search
        anchors { right: right.left; rightMargin: 20; top: parent.top }
        width: Math.min(300, root.width * 0.25)
        height: Theme.touch
        placeholderText: "Search tools…"
        placeholderTextColor: Theme.label
        color: Theme.text
        font.family: Theme.sans; font.pixelSize: 15
        leftPadding: 14
        verticalAlignment: TextInput.AlignVCenter
        background: Rectangle { radius: Theme.buttonRadius; color: "#0f1113"; border.width: search.activeFocus ? 2 : 1
                                border.color: search.activeFocus ? Qt.rgba(0.9, 0.91, 0.93, 0.6) : Theme.outline }
        Accessible.name: "Search tools"
        onTextChanged: tools.setQuery(text)
    }

    // ---- the table --------------------------------------------------------------------------------------------------------------
    Panel {
        id: table
        anchors { left: parent.left; right: right.left; rightMargin: 20; top: tierTabs.bottom; topMargin: 20; bottom: parent.bottom }
        padding: 0
        readonly property real colTool: Math.min(220, width * 0.22)
        readonly property real colTier: 96
        readonly property real colUsed: 112
        readonly property real colOn: 72
        readonly property real colWhat: width - 48 - colTool - colTier - colUsed - colOn
        Row {
            id: head
            x: 24; y: 18
            height: 24
            SectionLabel { width: table.colTool; text: "Tool" }
            SectionLabel { width: table.colWhat; text: "What it does" }
            SectionLabel { width: table.colTier; text: "Tier" }
            SectionLabel { width: table.colUsed; text: "Last used" }
            SectionLabel { width: table.colOn; text: "On" }
        }
        Rectangle { anchors { left: parent.left; right: parent.right; top: head.bottom; topMargin: 10 } height: 1; color: Theme.outline }
        ListView {
            id: list
            anchors { left: parent.left; right: parent.right; top: head.bottom; topMargin: 11; bottom: parent.bottom; bottomMargin: 6 }
            clip: true
            model: tools.tools
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            delegate: Item {
                required property var modelData
                required property int index
                width: list.width
                height: 58
                Rectangle { anchors.fill: parent; color: index % 2 ? "transparent" : Qt.rgba(1, 1, 1, 0.012) }
                Row {
                    x: 24
                    height: parent.height
                    opacity: modelData.enabled ? 1 : 0.55
                    Behavior on opacity { NumberAnimation { duration: Theme.motion } }
                    SText { width: table.colTool - 12; anchors.verticalCenter: parent.verticalCenter; mono: true; font.pixelSize: 14
                            text: modelData.name }
                    SText { width: table.colWhat - 16; anchors.verticalCenter: parent.verticalCenter; secondary: true; font.pixelSize: 13
                            text: modelData.description; wrapMode: Text.WordWrap; maximumLineCount: 2; lineHeight: 1.05 }
                    Item { width: table.colTier; height: parent.height
                           TierPill { tier: modelData.tier; anchors.verticalCenter: parent.verticalCenter } }
                    SText { width: table.colUsed; anchors.verticalCenter: parent.verticalCenter; mono: true; secondary: true; font.pixelSize: 13
                            text: modelData.lastUsed }
                }
                MetalSwitch {
                    x: 24 + table.colTool + table.colWhat + table.colTier + table.colUsed
                    anchors.verticalCenter: parent.verticalCenter
                    checked: modelData.enabled
                    busy: modelData.pending
                    accessibleName: modelData.name + (modelData.enabled ? " on" : " off")
                    onToggled: (on) => tools.setEnabled(modelData.name, on)
                }
                Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: "#1a1c20" }
            }
            SText { visible: list.count === 0; anchors.centerIn: parent; secondary: true; text: "No tools match." }
        }
    }

    // ---- right column: audit log + how tiers work -----------------------------------------------------------------------------------
    Column {
        id: right
        width: Math.min(400, root.width * 0.31)
        anchors { right: parent.right; top: parent.top; bottom: parent.bottom }
        spacing: 20
        Panel {
            id: auditPanel
            width: parent.width
            height: parent.height - tiersPanel.height - parent.spacing
            Item {
                anchors.fill: parent
                Row {
                    id: auditHead
                    width: parent.width
                    SectionLabel { text: "Audit log"; width: parent.width - undoButton.width; anchors.verticalCenter: parent.verticalCenter }
                    MetalButton {
                        id: undoButton
                        visible: tools.undoInfo.available
                        width: visible ? implicitWidth : 0
                        height: 40
                        text: tools.undoInfo.verb || "Undo"
                        accessibleName: (tools.undoInfo.verb || "Undo") + ": " + tools.undoInfo.label
                        onClicked: tools.undo()
                    }
                }
                SText {
                    id: undoNote
                    anchors.top: auditHead.bottom; anchors.topMargin: 6
                    width: parent.width
                    visible: tools.undoInfo.available
                    height: visible ? 18 : 0
                    secondary: true; font.pixelSize: 12
                    text: tools.undoInfo.available ? "“Undo that” would reverse: " + tools.undoInfo.label : ""
                }
                ListView {
                    id: auditList
                    anchors { top: undoNote.bottom; topMargin: 12; left: parent.left; right: parent.right; bottom: parent.bottom }
                    clip: true
                    spacing: 4
                    model: tools.audit
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    delegate: Item {
                        required property var modelData
                        width: auditList.width - 8
                        height: 50
                        readonly property bool isUndo: tools.undoInfo.available && modelData.id === tools.undoInfo.auditId
                        Rectangle { visible: isUndo; anchors.fill: parent; anchors.margins: -4; radius: 8; color: "#17191c"
                                    border.width: 1; border.color: Theme.outline }
                        Rectangle { width: 8; height: 8; radius: 4; y: 7; color: Theme.tierColor(modelData.tier) }
                        SText { x: 18; y: 1; mono: true; secondary: true; font.pixelSize: 12; text: modelData.time }
                        SText { x: 18 + 64; y: 0; width: parent.width - x; mono: true; font.pixelSize: 14
                                text: modelData.tool
                                color: modelData.ok ? Theme.text : Theme.secondary }
                        SText { x: 18 + 64; y: 21; width: parent.width - x; secondary: true; font.pixelSize: 12
                                text: modelData.decision + (modelData.via ? " (" + modelData.via + ")" : "") + (modelData.result ? " · " + modelData.result : "") }
                    }
                    SText { visible: auditList.count === 0; secondary: true; text: "Nothing has run yet." }
                }
            }
        }
        Panel {
            id: tiersPanel
            width: parent.width
            height: tiersCol.implicitHeight + 2 * padding
            Column {
                id: tiersCol
                width: parent.width
                spacing: 12
                SectionLabel { text: "How tiers work" }
                Repeater {
                    model: [
                        { tier: "green", text: "Runs at once: open an app, play music, search, remember." },
                        { tier: "yellow", text: "Asks first: “Should I…?”. Answer by voice, the hotkey or on screen." },
                        { tier: "red", text: "Always shows the exact command or path on Vex's screen. Never by voice, never from this app." }
                    ]
                    delegate: Row {
                        required property var modelData
                        width: parent.width
                        spacing: 12
                        TierPill { tier: modelData.tier; width: 78 }
                        SText { width: parent.width - 90; text: modelData.text; secondary: true; font.pixelSize: 13; wrapMode: Text.WordWrap
                                elide: Text.ElideNone }
                    }
                }
            }
        }
    }
}
