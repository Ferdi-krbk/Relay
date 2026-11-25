import QtQuick

MouseArea {
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    activeFocusOnTab: true
    signal invoke()
    onClicked: invoke()
    Keys.onReturnPressed: invoke()
    Keys.onSpacePressed: invoke()
    Accessible.role: Accessible.Button
    Accessible.onPressAction: if (enabled) invoke()
}
