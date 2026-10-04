import QtQuick
import QtQuick.Controls.Basic
import "../components"

// Budget: what Vex costs (one hero number), Groq's free daily tokens per model, and every model's use this week.
FocusScope {
    id: root
    Component.onCompleted: { budgetModel.refresh(); budgetModel.setLive(true) }
    Component.onDestruction: budgetModel.setLive(false)

    // ---- the money (the one hero number on this screen) ------------------------------------------------------------------------------
    Panel {
        id: moneyPanel
        anchors { left: parent.left; top: parent.top }
        width: (root.width - 20) * 0.42
        height: 250
        Column {
            width: parent.width
            spacing: 10
            SectionLabel { text: "Spent this month" }
            Row {
                spacing: 10
                SText { mono: true; font.pixelSize: 56; font.weight: Font.Light; text: "₹" + Math.round(budgetModel.money.used) }
                SText { mono: true; secondary: true; font.pixelSize: 18; anchors.baseline: parent.children[0].baseline
                        text: "of ₹" + Math.round(budgetModel.money.total) }
            }
            Rectangle {
                width: parent.width; height: 8; radius: 4; color: "#0f1113"; border.width: 1; border.color: Theme.outline
                Rectangle { width: parent.width * budgetModel.money.fraction; visible: width >= 2; height: parent.height; radius: 4
                            gradient: Gradient { orientation: Gradient.Horizontal
                                GradientStop { position: 0; color: Theme.chromeBottom } GradientStop { position: 1; color: Theme.chromeTop } } }
            }
            SText { width: parent.width; secondary: true; font.pixelSize: 13; wrapMode: Text.WordWrap; elide: Text.ElideNone
                    text: budgetModel.money.note }
            SText { width: parent.width; mono: true; secondary: true; font.pixelSize: 12; wrapMode: Text.WordWrap; elide: Text.ElideNone
                    text: "Google Cloud TTS " + (budgetModel.money.googleOn ? "on" : "off") + " · " + budgetModel.money.googleText
                          + " free characters used this month" }
        }
    }

    // ---- Groq's free daily tokens ------------------------------------------------------------------------------------------------------
    Panel {
        id: groqPanel
        anchors { left: moneyPanel.right; leftMargin: 20; right: parent.right; top: parent.top }
        height: 250
        Item {
            anchors.fill: parent
            Column {
                id: gh
                spacing: 2
                SectionLabel { text: "Groq tokens today" }
                SText { secondary: true; font.pixelSize: 12; text: "Free tier: 200K tokens per model per day. Past it, Vex moves to the next model." }
            }
            Column {
                anchors { top: gh.bottom; topMargin: 18; left: parent.left; right: parent.right }
                spacing: 18
                Repeater {
                    model: budgetModel.groqRows
                    delegate: Column {
                        required property var modelData
                        width: parent.width
                        spacing: 6
                        Row {
                            width: parent.width
                            SText { mono: true; font.pixelSize: 14; width: parent.width - used.width; text: modelData.provider + "  " + modelData.model }
                            SText { id: used; mono: true; font.pixelSize: 14; text: modelData.tokensText + " / " + modelData.limitText }
                        }
                        Rectangle {
                            width: parent.width; height: 10; radius: 5; color: "#0f1113"; border.width: 1; border.color: Theme.outline
                            Rectangle {
                                width: parent.width * modelData.fraction; visible: width >= 2; height: parent.height; radius: 5
                                color: modelData.severity ? Theme.tierColor(modelData.severity) : "#9aa1ab"
                                Behavior on width { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
                            }
                        }
                        SText { visible: modelData.level !== ""; font.pixelSize: 12; font.weight: Font.DemiBold
                                text: modelData.level.charAt(0).toUpperCase() + modelData.level.slice(1) }   // words, not colour alone
                    }
                }
                SText { visible: budgetModel.groqRows.length === 0; secondary: true; text: "No Groq requests counted yet today." }
            }
        }
    }

    // ---- every model this week ----------------------------------------------------------------------------------------------------------
    Panel {
        id: table
        anchors { left: parent.left; right: parent.right; top: moneyPanel.bottom; topMargin: 20; bottom: parent.bottom }
        padding: 0
        readonly property real cModel: width * 0.28
        readonly property real cReq: 110
        readonly property real cTok: 120
        readonly property real cSpend: 140
        readonly property real cWeek: width - 48 - cModel - cReq - cTok - cSpend
        Row {
            id: th
            x: 24; y: 18
            SectionLabel { width: table.cModel; text: "Model" }
            SectionLabel { width: table.cReq; text: "Requests" }
            SectionLabel { width: table.cTok; text: "Tokens" }
            SectionLabel { width: table.cWeek; text: "Last 7 days" }
            SectionLabel { width: table.cSpend; text: "Spend" }
        }
        SText { x: 24 + table.cModel; y: 38; secondary: true; font.pixelSize: 11; text: "today" }
        SText { x: 24 + table.cModel + table.cReq; y: 38; secondary: true; font.pixelSize: 11; text: "today" }
        Rectangle { anchors { left: parent.left; right: parent.right } y: 60; height: 1; color: Theme.outline }
        ListView {
            id: rows
            anchors { left: parent.left; right: parent.right; top: parent.top; topMargin: 61; bottom: parent.bottom; bottomMargin: 6 }
            clip: true
            model: budgetModel.rows
            boundsBehavior: Flickable.StopAtBounds
            delegate: Item {
                id: rowItem
                required property var modelData
                property string hoverText: ""
                width: rows.width
                height: 64
                Row {
                    x: 24; height: parent.height
                    Column { width: table.cModel; anchors.verticalCenter: parent.verticalCenter; spacing: 2
                             SText { mono: true; font.pixelSize: 14; text: modelData.provider; width: parent.width - 12 }
                             SText { mono: true; secondary: true; font.pixelSize: 12; text: modelData.model; width: parent.width - 12 } }
                    SText { width: table.cReq; anchors.verticalCenter: parent.verticalCenter; mono: true; font.pixelSize: 14; text: modelData.requests }
                    SText { width: table.cTok; anchors.verticalCenter: parent.verticalCenter; mono: true; font.pixelSize: 14; text: modelData.tokensText }
                    Item {
                        width: table.cWeek; height: parent.height
                        Row {                                        // small multiples: one shared scale for every model
                            id: week
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 6
                            Repeater {
                                model: rowItem.modelData.week
                                delegate: Item {
                                    required property var modelData
                                    width: 14; height: 34
                                    Rectangle { anchors.bottom: parent.bottom; width: 10; anchors.horizontalCenter: parent.horizontalCenter
                                                height: Math.max(2, modelData.h * 34); radius: 2
                                                color: hov.containsMouse ? Theme.text : "#7c828b" }
                                    MouseArea { id: hov; anchors.fill: parent; anchors.margins: -3; hoverEnabled: true
                                                onEntered: rowItem.hoverText = modelData.day + ": " + modelData.text
                                                onExited: rowItem.hoverText = "" }
                                }
                            }
                        }
                        SText { anchors.left: week.right; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - week.width - 16; mono: true; secondary: true; font.pixelSize: 12
                                text: rowItem.hoverText || ("week " + rowItem.modelData.weekTotal) }
                    }
                    SText { width: table.cSpend; anchors.verticalCenter: parent.verticalCenter; mono: true; secondary: true; font.pixelSize: 13
                            text: modelData.spend }
                }
                Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: "#1a1c20" }
            }
            SText { visible: rows.count === 0; anchors.centerIn: parent; secondary: true
                    text: "No model use counted yet. Counting starts with this version of Vex." }
        }
    }
}
