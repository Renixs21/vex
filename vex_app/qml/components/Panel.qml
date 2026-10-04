import QtQuick

// Brushed gunmetal panel (shaders/panel.frag): gradient, faint vertical brush lines, 1px outline, 1px top highlight, radius 16.
Item {
    id: root
    property real radius: Theme.radius
    property real padding: 20
    default property alias content: inner.data
    property alias inner: inner

    ShaderEffect {
        anchors.fill: parent
        property vector2d boxSize: Qt.vector2d(width, height)
        property real radius: root.radius
        property color topColor: Theme.panelTop
        property color bottomColor: Theme.panelBottom
        property color outlineColor: Theme.outline
        property real highlight: 0.07
        property real brush: 1.0
        fragmentShader: appPaths.shaders + "panel.frag.qsb"
    }
    Item {
        id: inner
        anchors.fill: parent
        anchors.margins: root.padding
    }
}
