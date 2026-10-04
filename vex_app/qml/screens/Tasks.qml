import QtQuick
import QtQuick.Controls.Basic
import "../components"

// Tasks & reminders: the to-do list (VexVault/tasks.md, editable by hand too) and the reminders Vex will speak. Changes go through
// Vex's own tools: audited, and deleting a task asks first on Vex's screen.
FocusScope {
    id: root
    Component.onCompleted: tasksModel.refresh()

    Row {
        anchors.fill: parent
        spacing: 20

        // ---- tasks --------------------------------------------------------------------------------------------------------------
        Panel {
            width: (parent.width - parent.spacing) * 0.55
            height: parent.height
            Item {
                anchors.fill: parent
                Row {
                    id: taskHead
                    width: parent.width
                    SectionLabel { text: "Tasks"; width: parent.width - openText.width }
                    SText { id: openText; mono: true; secondary: true; font.pixelSize: 13; text: tasksModel.openCount + " open" }
                }
                Row {
                    id: addRow
                    anchors { top: taskHead.bottom; topMargin: 14 }
                    width: parent.width
                    spacing: 10
                    Field { id: taskText; width: parent.width - taskDue.width - addTask.width - 20; placeholderText: "Add a task…"
                            onAccepted: addTask.clicked() }
                    Field { id: taskDue; width: 150; placeholderText: "Due (optional)"; label: "Due date"; onAccepted: addTask.clicked() }
                    MetalButton {
                        id: addTask
                        chrome: true; text: "Add"; width: 84
                        enabled: taskText.text.trim().length > 0
                        onClicked: { tasksModel.addTask(taskText.text, taskDue.text); taskText.text = ""; taskDue.text = ""; taskText.forceActiveFocus() }
                    }
                }
                ListView {
                    id: taskList
                    anchors { top: addRow.bottom; topMargin: 16; left: parent.left; right: parent.right; bottom: parent.bottom }
                    clip: true
                    spacing: 2
                    model: tasksModel.tasks
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    delegate: Item {
                        required property var modelData
                        width: taskList.width - 8
                        height: 52
                        opacity: modelData.done ? 0.5 : 1
                        // a machined check box (44 px target)
                        Item {
                            id: check
                            width: Theme.touch; height: Theme.touch
                            anchors.verticalCenter: parent.verticalCenter
                            activeFocusOnTab: !modelData.done
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: "Done: " + modelData.text
                            Accessible.checked: modelData.done
                            Keys.onSpacePressed: if (!modelData.done) tasksModel.completeTask(modelData.number)
                            Rectangle {
                                width: 22; height: 22; radius: 6
                                anchors.centerIn: parent
                                border.width: 1; border.color: modelData.done ? "#9aa1ab" : Theme.buttonOutline
                                gradient: Gradient {
                                    GradientStop { position: 0; color: modelData.done ? Theme.chromeTop : Theme.buttonTop }
                                    GradientStop { position: 1; color: modelData.done ? Theme.chromeBottom : Theme.buttonBottom }
                                }
                                Text { anchors.centerIn: parent; visible: modelData.done; text: "✓"; color: Theme.chromeText
                                       font.pixelSize: 14; font.weight: Font.Bold }
                            }
                            Rectangle { anchors.centerIn: parent; width: 30; height: 30; radius: 9; color: "transparent"; border.width: 2
                                        border.color: Theme.focus; opacity: check.activeFocus ? 0.75 : 0 }
                            MouseArea { anchors.fill: parent; cursorShape: modelData.done ? Qt.ArrowCursor : Qt.PointingHandCursor
                                        onClicked: if (!modelData.done) tasksModel.completeTask(modelData.number) }
                        }
                        SText {
                            x: Theme.touch + 6; anchors.verticalCenter: parent.verticalCenter
                            width: parent.width - x - dueText.width - delButton.width - 24
                            text: modelData.text; font.pixelSize: 15; font.strikeout: modelData.done
                        }
                        SText { id: dueText; anchors.right: delButton.left; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
                                mono: true; font.pixelSize: 13; text: modelData.due
                                color: modelData.overdue ? Theme.text : Theme.secondary
                                font.weight: modelData.overdue ? Font.DemiBold : Font.Normal }
                        MetalButton {
                            id: delButton
                            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                            text: "Delete"; height: 36
                            visible: !modelData.done
                            onClicked: tasksModel.deleteTask(modelData.number)
                        }
                    }
                    SText { visible: taskList.count === 0; anchors.centerIn: parent; secondary: true; text: "No tasks. Add one above, or say “add a task…”." }
                }
            }
        }

        // ---- reminders ----------------------------------------------------------------------------------------------------------
        Panel {
            width: (parent.width - parent.spacing) * 0.45
            height: parent.height
            Item {
                anchors.fill: parent
                SectionLabel { id: remHead; text: "Reminders" }
                Column {
                    id: remAdd
                    anchors { top: remHead.bottom; topMargin: 14 }
                    width: parent.width
                    spacing: 10
                    Field { id: remText; width: parent.width; placeholderText: "Remind me to…"; onAccepted: remWhen.forceActiveFocus() }
                    Row {
                        width: parent.width
                        spacing: 10
                        Field { id: remWhen; width: parent.width - addRem.width - 10; placeholderText: "When (e.g. tomorrow 9am)"
                                label: "When"; onAccepted: addRem.clicked() }
                        MetalButton {
                            id: addRem
                            chrome: true; text: "Set"; width: 84
                            enabled: remText.text.trim().length > 0 && remWhen.text.trim().length > 0
                            onClicked: { tasksModel.addReminder(remText.text, remWhen.text); remText.text = ""; remWhen.text = ""; remText.forceActiveFocus() }
                        }
                    }
                }
                ListView {
                    id: remList
                    anchors { top: remAdd.bottom; topMargin: 16; left: parent.left; right: parent.right; bottom: parent.bottom }
                    clip: true
                    spacing: 4
                    model: tasksModel.reminders
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    delegate: Item {
                        required property var modelData
                        width: remList.width - 8
                        height: 58
                        SText { y: 8; width: parent.width - cancel.width - 12; mono: true; font.pixelSize: 13; text: modelData.when }
                        SText { y: 30; width: parent.width - cancel.width - 12; secondary: true; font.pixelSize: 14
                                text: modelData.text + (modelData.repeat ? "  ·  " + modelData.repeat : "") }
                        MetalButton { id: cancel; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                                      text: "Cancel"; height: 36; onClicked: tasksModel.cancelReminder(modelData.number) }
                    }
                    SText { visible: remList.count === 0; anchors.centerIn: parent; secondary: true; text: "No reminders set." }
                }
            }
        }
    }
}
