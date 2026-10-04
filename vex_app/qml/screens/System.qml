import QtQuick
import QtQuick.Controls.Basic
import "../components"

// System & health: the health check (every part, in plain words), Vex itself (start with Windows, restart), and Vex's own logs.
FocusScope {
    id: root
    Component.onCompleted: systemModel.refresh()

    // ---- health ---------------------------------------------------------------------------------------------------------------------
    Panel {
        id: healthPanel
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
        width: (root.width - 20) * 0.46
        Item {
            anchors.fill: parent
            Row {
                id: hh
                width: parent.width
                Column {
                    width: parent.width - runButton.width
                    spacing: 4
                    SectionLabel { text: "Health" }
                    SText { width: parent.width - 12; text: systemModel.healthSummary; font.pixelSize: 14; wrapMode: Text.WordWrap
                            elide: Text.ElideNone; maximumLineCount: 2 }
                    SText { visible: systemModel.healthAt !== ""; mono: true; secondary: true; font.pixelSize: 12
                            text: "checked " + systemModel.healthAt + " · no API quota used" }
                }
                MetalButton { id: runButton; chrome: true; width: 132; enabled: !systemModel.healthBusy
                              text: systemModel.healthBusy ? "Checking…" : "Run check"; onClicked: systemModel.runHealth() }
            }
            ListView {
                id: checkList
                anchors { top: hh.bottom; topMargin: 16; left: parent.left; right: parent.right; bottom: parent.bottom }
                clip: true
                spacing: 2
                model: systemModel.checks
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                delegate: Item {
                    required property var modelData
                    width: checkList.width - 8
                    height: modelData.hint && modelData.status !== "PASS" ? 66 : 46
                    StatusBadge { y: 8; status: modelData.status }
                    SText { x: 70; y: 6; width: parent.width - 70; font.pixelSize: 14; text: modelData.name }
                    SText { x: 70; y: 25; width: parent.width - 70; secondary: true; font.pixelSize: 12; text: modelData.detail }
                    SText { x: 70; y: 44; width: parent.width - 70; visible: modelData.hint && modelData.status !== "PASS"
                            font.pixelSize: 12; text: "→ " + modelData.hint }
                }
                SText { visible: checkList.count === 0; anchors.centerIn: parent; secondary: true; width: parent.width * 0.8
                        horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap; elide: Text.ElideNone
                        text: "Run the check to see every part of Vex: brains, speech, microphone, wake word, memory, disk. It takes about 5 seconds and uses no API quota." }
            }
        }
    }

    // ---- Vex ---------------------------------------------------------------------------------------------------------------------------
    Panel {
        id: vexPanel
        anchors { left: healthPanel.right; leftMargin: 20; right: parent.right; top: parent.top }
        height: 196
        Column {
            width: parent.width
            spacing: 12
            Row {
                width: parent.width
                SectionLabel { text: "Vex"; width: parent.width - info.width }
                SText { id: info; mono: true; secondary: true; font.pixelSize: 12
                        text: (systemModel.info.commit ? systemModel.info.commit + " · " : "") + "Python " + (systemModel.info.python || "")
                              + " · pid " + (systemModel.info.pid || "") }
            }
            Row {
                width: parent.width
                Column {
                    width: parent.width - asw.width
                    SText { text: "Start with Windows"; font.pixelSize: 15 }
                    SText { secondary: true; font.pixelSize: 12; width: parent.width - 12; wrapMode: Text.WordWrap; elide: Text.ElideNone
                            text: "Quietly, 15 s after you log in. Turned off, it stays off." }
                }
                MetalSwitch { id: asw; checked: systemModel.autostartOn; accessibleName: "Start with Windows"
                              onToggled: (on) => systemModel.setAutostart(on) }
            }
            Row {
                spacing: 10
                MetalButton { text: systemModel.restarting ? "Restarting…" : "Restart Vex"; enabled: !systemModel.restarting
                              onClicked: restartDialog.open() }
                MetalButton { text: "Open logs folder"; onClicked: systemModel.openLogs() }
                SText { anchors.verticalCenter: parent.verticalCenter; secondary: true; font.pixelSize: 12
                        text: systemModel.info.supervised ? "kept running by the supervisor" : "started by hand" }
            }
        }
    }

    // ---- logs ---------------------------------------------------------------------------------------------------------------------------
    Panel {
        anchors { left: healthPanel.right; leftMargin: 20; right: parent.right; top: vexPanel.bottom; topMargin: 20; bottom: parent.bottom }
        Item {
            anchors.fill: parent
            SectionLabel { id: logsLabel; text: "Logs" }
            Row {
                id: lh
                anchors.top: logsLabel.bottom; anchors.topMargin: 10
                width: parent.width
                spacing: 10
                SegmentedTabs {
                    readonly property var keys: systemModel.logs.map(l => l.name)
                    minTab: 60
                    tabs: keys
                    current: Math.max(0, keys.indexOf(systemModel.logName))
                    onPicked: (i) => systemModel.loadLog(keys[i])
                }
                MetalButton { text: "Refresh"; height: Theme.touch; onClicked: systemModel.loadLog(systemModel.logName) }
            }
            ScrollView {
                id: logScroll
                anchors { top: lh.bottom; topMargin: 14; left: parent.left; right: parent.right; bottom: parent.bottom }
                clip: true
                TextArea {
                    readOnly: true
                    text: systemModel.logText
                    color: Theme.secondary
                    font.family: Theme.mono
                    font.pixelSize: 11
                    wrapMode: TextEdit.NoWrap
                    selectByMouse: true
                    background: Rectangle { radius: Theme.buttonRadius; color: "#0f1113"; border.width: 1; border.color: Theme.outline }
                    Accessible.name: "Log text"
                    onTextChanged: cursorPosition = length                      // newest lines in view
                }
            }
        }
    }

    Dialog {
        id: restartDialog
        title: "Restart Vex?"
        sheetWidth: 480; sheetHeight: 230
        SText { width: parent.width; secondary: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; font.pixelSize: 14
                text: "Vex stops and starts again; it's back in a few seconds. Anything it's saying right now is cut off." }
        Row {
            anchors { right: parent.right; bottom: parent.bottom }
            spacing: 10
            MetalButton { id: stay; text: "Cancel"; onClicked: restartDialog.close() }
            MetalButton { chrome: true; text: "Restart"; width: 110; onClicked: { systemModel.restart(); restartDialog.close() } }
        }
        onVisibleChanged: if (visible) stay.forceActiveFocus()
    }
}
