import QtQuick
import QtQuick.Controls.Basic

// Response time per voice turn (one series, so no legend: the title names it). Thin neutral columns (<= 24 px, 4 px rounded tops,
// square at the baseline), hairline solid gridlines, clean second ticks, a median reference line. Hover or arrow keys show one turn;
// the Table toggle shows every number as text.
FocusScope {
    id: root
    property var rows: []                // [{ts, source, total, stt, brain, first_audio, followup}]
    property bool table: false
    property int current: -1
    readonly property real maxValue: {
        let m = 1
        for (let i = 0; i < rows.length; i++) m = Math.max(m, rows[i].total || 0)
        return Math.ceil(m / 2) * 2               // clean ticks: 0, 2, 4, 6 s...
    }
    readonly property real median: {
        const v = rows.map(r => r.total).filter(x => x !== undefined && x !== null).sort((a, b) => a - b)
        return v.length ? v[Math.floor(v.length / 2)] : 0
    }
    activeFocusOnTab: true
    Accessible.role: Accessible.Chart
    Accessible.name: "Response time per voice turn, median " + median.toFixed(1) + " seconds"
    Keys.onLeftPressed: current = Math.max(0, current < 0 ? rows.length - 1 : current - 1)
    Keys.onRightPressed: current = Math.min(rows.length - 1, current + 1)
    Keys.onEscapePressed: current = -1

    function hhmm(ts) { return ts ? String(ts).substring(11, 16) : "" }
    function fmt(v) { return (v === undefined || v === null) ? "—" : Number(v).toFixed(1) }

    readonly property real axisW: 40
    readonly property real plotX: axisW
    readonly property real plotW: width - axisW - 8
    readonly property real plotH: height - 26
    readonly property real slot: rows.length ? plotW / rows.length : plotW

    Item {
        anchors.fill: parent
        visible: !root.table
        // gridlines + ticks
        Repeater {
            model: root.maxValue / 2 + 1
            delegate: Item {
                required property int index
                readonly property real v: index * 2
                y: root.plotH - (v / root.maxValue) * root.plotH
                width: root.width; height: 1
                Rectangle { x: root.plotX; width: root.plotW; height: 1; color: index === 0 ? "#2c3036" : "#1f2226" }
                Text { x: 0; width: root.axisW - 10; y: -8; horizontalAlignment: Text.AlignRight; text: v + " s"
                       color: Theme.label; font.family: Theme.mono; font.pixelSize: 11; renderType: Text.NativeRendering }
            }
        }
        // columns
        Repeater {
            model: root.rows
            delegate: Item {
                required property var modelData
                required property int index
                readonly property bool on: index === root.current
                x: root.plotX + index * root.slot
                width: root.slot
                height: root.plotH
                Rectangle {
                    id: col
                    readonly property real h: Math.max(2, (modelData.total / root.maxValue) * root.plotH)
                    width: Math.min(24, Math.max(3, root.slot * 0.62))
                    height: h
                    anchors.horizontalCenter: parent.horizontalCenter
                    y: root.plotH - h
                    radius: Math.min(4, width / 2)
                    color: on ? Theme.text : "#9aa1ab"
                    Behavior on color { ColorAnimation { duration: 140 } }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: Math.min(4, parent.height); color: parent.color }
                }
                MouseArea {                            // the hit target is the whole column, bigger than the mark
                    anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.current = index
                    onExited: if (root.current === index && !root.activeFocus) root.current = -1
                }
            }
        }
        // median reference line + label
        Rectangle {
            visible: root.rows.length > 1
            x: root.plotX; width: root.plotW; height: 1
            y: root.plotH - (root.median / root.maxValue) * root.plotH
            color: "#5a5f67"
            z: 2
            Rectangle {                                 // the label sits on a surface chip above the columns, never over a bar's ink
                x: 6; anchors.bottom: parent.top; anchors.bottomMargin: 3
                width: medianText.implicitWidth + 12; height: medianText.implicitHeight + 4; radius: 4
                color: Theme.panelBottom; border.width: 1; border.color: Theme.outline
                Text { id: medianText; anchors.centerIn: parent; text: "median " + root.median.toFixed(1) + " s"; color: Theme.secondary
                       font.family: Theme.mono; font.pixelSize: 11; renderType: Text.NativeRendering }
            }
        }
        // x labels: first and last turn only (selective)
        Text { visible: root.rows.length > 0; x: root.plotX; y: root.plotH + 8; text: root.hhmm(root.rows.length ? root.rows[0].ts : "")
               color: Theme.label; font.family: Theme.mono; font.pixelSize: 11; renderType: Text.NativeRendering }
        Text { visible: root.rows.length > 1; anchors.right: parent.right; anchors.rightMargin: 8; y: root.plotH + 8
               text: root.hhmm(root.rows.length ? root.rows[root.rows.length - 1].ts : ""); color: Theme.label; font.family: Theme.mono
               font.pixelSize: 11; renderType: Text.NativeRendering }
        SText { visible: root.rows.length === 0; anchors.centerIn: parent; secondary: true; text: "No voice turns timed yet." }

        // tooltip: value first, then when / how, then the stages
        Panel {
            id: tip
            visible: root.current >= 0 && root.current < root.rows.length
            readonly property var r: visible ? root.rows[root.current] : ({})
            width: 236; height: 92; padding: 12; radius: 12
            x: Math.max(0, Math.min(root.width - width, root.plotX + (root.current + 0.5) * root.slot - width / 2))
            y: 0
            z: 5
            Column {
                spacing: 4
                SText { mono: true; font.pixelSize: 18; font.weight: Font.DemiBold; text: root.fmt(tip.r.total) + " s" }
                SText { mono: true; secondary: true; font.pixelSize: 12
                        text: root.hhmm(tip.r.ts) + " · " + (tip.r.source || "") }
                SText { mono: true; secondary: true; font.pixelSize: 12
                        text: "speech " + root.fmt(tip.r.stt) + " · brain " + root.fmt(tip.r.brain) + " · sound " + root.fmt(tip.r.first_audio) }
            }
        }
        Rectangle { anchors.fill: parent; anchors.margins: -4; radius: 8; color: "transparent"; border.width: 2; border.color: Theme.focus
                    opacity: root.activeFocus ? 0.5 : 0 }
    }

    // the table view: every value as text
    ListView {
        anchors.fill: parent
        visible: root.table
        clip: true
        model: root.rows.slice().reverse()
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        header: Row {
            height: 26; spacing: 0
            Repeater { model: ["Time", "Via", "Total", "Speech", "Brain", "First sound"]
                       delegate: SectionLabel { required property var modelData; width: 92; text: modelData; font.pixelSize: 11 } }
        }
        delegate: Row {
            required property var modelData
            height: 26
            Repeater {
                model: [root.hhmm(modelData.ts), modelData.source || "", root.fmt(modelData.total), root.fmt(modelData.stt),
                        root.fmt(modelData.brain), root.fmt(modelData.first_audio)]
                delegate: SText { required property var modelData; width: 92; mono: true; font.pixelSize: 12; text: modelData }
            }
        }
    }
}
