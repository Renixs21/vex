import QtQuick
import QtQuick.Controls.Basic
import "../components"

// Voice: wake word sensitivity (with a live mic meter and the wake score against the threshold), the follow-up window, Vex's voice,
// and response time per voice turn.
FocusScope {
    id: root
    Component.onCompleted: { voiceModel.refresh(); voiceModel.setLive(true) }
    Component.onDestruction: voiceModel.setLive(false)

    readonly property real topH: Math.min(430, height * 0.56)

    // ---- wake word ----------------------------------------------------------------------------------------------------------------
    Panel {
        id: wakePanel
        anchors { left: parent.left; top: parent.top }
        width: (root.width - 40) * 0.36
        height: root.topH
        Column {
            width: parent.width
            spacing: 14
            Row {
                width: parent.width
                SectionLabel { text: "Wake word"; width: parent.width - wl.width }
                SText { id: wl; mono: true; secondary: true; font.pixelSize: 13; text: voiceModel.wakeLabel }
            }
            SText { width: parent.width; secondary: true; font.pixelSize: 13; wrapMode: Text.WordWrap; elide: Text.ElideNone
                    text: "Say “Hey Vex” and watch the score. When it crosses the line, Vex wakes up." }
            // live meters
            Column {
                width: parent.width
                spacing: 6
                Row { width: parent.width
                      SText { text: "Microphone"; font.pixelSize: 13; secondary: true; width: parent.width - micState.width }
                      SText { id: micState; mono: true; font.pixelSize: 12; secondary: true
                              text: voiceModel.listening ? "listening" : "paused" } }
                Rectangle {
                    width: parent.width; height: 10; radius: 5; color: "#0f1113"; border.width: 1; border.color: Theme.outline
                    Rectangle { width: Math.max(0, parent.width * voiceModel.level); height: parent.height; radius: 5; color: "#9aa1ab"
                                Behavior on width { NumberAnimation { duration: 110 } } }
                }
                SText { text: "Wake score"; font.pixelSize: 13; secondary: true; topPadding: 6 }
                Item {
                    width: parent.width; height: 10
                    Rectangle {
                        anchors.fill: parent; radius: 5; color: "#0f1113"; border.width: 1; border.color: Theme.outline
                        Rectangle { width: Math.max(0, parent.width * Math.min(1, voiceModel.score)); height: parent.height; radius: 5
                                    color: voiceModel.score >= voiceModel.threshold ? Theme.text : "#9aa1ab"
                                    Behavior on width { NumberAnimation { duration: 110 } } }
                    }
                    Rectangle { x: parent.width * voiceModel.threshold - 1; y: -4; width: 2; height: 18; radius: 1; color: Theme.chromeTop }
                }
            }
            // sensitivity (a lower threshold wakes more easily)
            Column {
                width: parent.width
                spacing: 2
                Row { width: parent.width
                      SText { text: "Sensitivity"; font.pixelSize: 14; width: parent.width - thr.width }
                      SText { id: thr; mono: true; secondary: true; font.pixelSize: 13; text: "threshold " + sens.threshold.toFixed(2) } }
                MetalSlider {
                    id: sens
                    width: parent.width
                    from: 0.05; to: 0.95; stepSize: 0.01
                    readonly property real threshold: 1.0 - value + 0.0          // the slider shows sensitivity: right = wakes more easily
                    value: 1.0 - voiceModel.threshold
                    enabled: voiceModel.wakeAvailable
                    accessibleName: "Wake word sensitivity"
                    onPressedChanged: if (!pressed) voiceModel.setThreshold(1.0 - value)
                    Keys.onReleased: (e) => { if (e.key === Qt.Key_Left || e.key === Qt.Key_Right) voiceModel.setThreshold(1.0 - value) }
                }
                Row { width: parent.width
                      SText { text: "fewer false wakes"; secondary: true; font.pixelSize: 12; width: parent.width - more.width }
                      SText { id: more; text: "wakes more easily"; secondary: true; font.pixelSize: 12 } }
            }
        }
    }

    // ---- follow-up -----------------------------------------------------------------------------------------------------------------
    Panel {
        id: followPanel
        anchors { left: wakePanel.right; leftMargin: 20; top: parent.top }
        width: (root.width - 40) * 0.28
        height: root.topH
        Column {
            width: parent.width
            spacing: 14
            Row {
                width: parent.width
                SectionLabel { text: "Follow-up"; width: parent.width - fsw.width; anchors.verticalCenter: parent.verticalCenter }
                MetalSwitch { id: fsw; checked: voiceModel.followupEnabled; accessibleName: "Follow-up listening"
                              onToggled: (on) => voiceModel.setFollowupEnabled(on) }
            }
            SText { width: parent.width; secondary: true; font.pixelSize: 13; wrapMode: Text.WordWrap; elide: Text.ElideNone
                    text: "After a reply, Vex keeps listening this long for the next thing, no “Hey Vex” needed." }
            Row {
                spacing: 12
                opacity: voiceModel.followupEnabled ? 1 : 0.45
                MetalButton { text: "−"; mono: true; width: Theme.touch; accessibleName: "Shorter"
                              enabled: voiceModel.followupEnabled && voiceModel.followupSeconds > 2
                              onClicked: voiceModel.setFollowupSeconds(voiceModel.followupSeconds - 1) }
                SText { mono: true; font.pixelSize: 30; font.weight: Font.Light; text: voiceModel.followupSeconds + " s"
                        anchors.verticalCenter: parent.verticalCenter; width: 86; horizontalAlignment: Text.AlignHCenter }
                MetalButton { text: "+"; mono: true; width: Theme.touch; accessibleName: "Longer"
                              enabled: voiceModel.followupEnabled && voiceModel.followupSeconds < 30
                              onClicked: voiceModel.setFollowupSeconds(voiceModel.followupSeconds + 1) }
            }
        }
    }

    // ---- voice picker --------------------------------------------------------------------------------------------------------------
    Panel {
        id: voicePanel
        anchors { left: followPanel.right; leftMargin: 20; right: parent.right; top: parent.top }
        height: root.topH
        Item {
            anchors.fill: parent
            SectionLabel { id: vh; text: "Vex's voice" }
            ListView {
                anchors { top: vh.bottom; topMargin: 12; left: parent.left; right: parent.right; bottom: parent.bottom }
                clip: true
                spacing: 4
                model: voiceModel.voices
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                delegate: Item {
                    required property var modelData
                    readonly property bool on: modelData.name === voiceModel.voice
                    width: ListView.view.width - 8
                    height: 52
                    Rectangle { anchors.fill: parent; radius: Theme.buttonRadius; color: on ? "#1d2024" : "transparent"
                                border.width: on ? 1 : 0; border.color: Theme.outline }
                    Item {
                        id: radio
                        width: Theme.touch; height: Theme.touch; anchors.verticalCenter: parent.verticalCenter
                        activeFocusOnTab: true
                        Accessible.role: Accessible.RadioButton; Accessible.name: modelData.label; Accessible.checked: on
                        Keys.onSpacePressed: voiceModel.setVoice(modelData.name)
                        Rectangle { anchors.centerIn: parent; width: 20; height: 20; radius: 10; border.width: 1
                                    border.color: on ? "#9aa1ab" : Theme.buttonOutline; color: "#0f1113"
                                    Rectangle { anchors.centerIn: parent; width: 10; height: 10; radius: 5; visible: on
                                                gradient: Gradient { GradientStop { position: 0; color: Theme.chromeTop }
                                                                     GradientStop { position: 1; color: Theme.chromeBottom } } } }
                        Rectangle { anchors.centerIn: parent; width: 28; height: 28; radius: 14; color: "transparent"; border.width: 2
                                    border.color: Theme.focus; opacity: radio.activeFocus ? 0.75 : 0 }
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: voiceModel.setVoice(modelData.name) }
                    }
                    SText { x: Theme.touch + 4; y: 8; text: modelData.label; font.pixelSize: 15 }
                    SText { x: Theme.touch + 4; y: 28; mono: true; secondary: true; font.pixelSize: 11; text: modelData.region }
                    MetalButton { anchors.right: parent.right; anchors.rightMargin: 6; anchors.verticalCenter: parent.verticalCenter
                                  height: 36; text: "Preview"; accessibleName: "Preview " + modelData.label
                                  onClicked: voiceModel.preview(modelData.name) }
                }
            }
        }
    }

    // ---- response time ------------------------------------------------------------------------------------------------------------
    Panel {
        anchors { left: parent.left; right: parent.right; top: wakePanel.bottom; topMargin: 20; bottom: parent.bottom }
        Item {
            anchors.fill: parent
            Row {
                id: ch
                width: parent.width
                Column {
                    width: parent.width - tableToggle.width
                    spacing: 2
                    SectionLabel { text: "Response time per voice turn" }
                    SText { secondary: true; font.pixelSize: 12; text: "From when you stop talking to Vex's first word. Last " + voiceModel.turns.length + " turns." }
                }
                MetalButton { id: tableToggle; height: 36; text: chart.table ? "Chart" : "Table"; checked: chart.table
                              onClicked: chart.table = !chart.table }
            }
            LatencyChart {
                id: chart
                anchors { top: ch.bottom; topMargin: 14; left: parent.left; right: parent.right; bottom: parent.bottom }
                rows: voiceModel.turns
            }
        }
    }
}
