import QtQuick

// Labels: uppercase, tracked 0.14em, 13 px, #8d939c.
Text {
    color: Theme.label
    font.family: Theme.sans
    font.pixelSize: 13
    font.weight: Font.Medium
    font.capitalization: Font.AllUppercase
    font.letterSpacing: Theme.labelTracking
    renderType: Text.NativeRendering
}
