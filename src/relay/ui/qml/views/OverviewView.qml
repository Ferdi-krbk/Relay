import QtQuick
import QtQuick.Layouts
import QtQuick.Shapes
import "../components"

Item {
    id: root
    required property var theme
    required property bool motion
    required property real spinAngle
    required property real connectRipple

    property alias leftColumn: leftHero
    property alias leftShift: leftShiftTransform
    property alias rightColumn: rightCard
    property alias rightShift: rightShiftTransform
    property alias rightScale: rightScaleTransform

    readonly property int modeIndex: network.mode === "auto" ? 0
        : network.mode === "strict" ? 1
        : network.mode === "fast" ? 2 : 3

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 26
        anchors.rightMargin: 26
        anchors.topMargin: 22
        anchors.bottomMargin: 24
        spacing: 24

        // LEFT COLUMN: Editorial Hero + Tactile CTA + Sliding Profile Selector
        ColumnLayout {
            id: leftHero
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0
            transform: Translate { id: leftShiftTransform; y: 0 }

            Row {
                spacing: 7
                Rectangle {
                    width: 7; height: 7; radius: 3.5; antialiasing: true
                    anchors.verticalCenter: parent.verticalCenter
                    color: network.error.length > 0 ? root.theme.vermilion
                        : network.connected ? root.theme.vermilion
                        : network.busy ? root.theme.ink
                        : root.theme.inkMuted
                    Behavior on color { ColorAnimation { duration: 220 } }
                }
                BodyText {
                    text: network.busy
                        ? (network.disconnecting ? "BAĞLANTI KESİLİYOR" : "BAĞLANIYOR")
                        : (network.connected ? "AKTİF KORUMA" : "ÇEVRİMDIŞI")
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.3
                    color: network.connected ? root.theme.vermilion : root.theme.inkMuted
                    Behavior on color { ColorAnimation { duration: 220 } }
                }
            }

            Item { Layout.preferredHeight: 12 }

            DisplayText {
                id: heroHeadline
                Layout.fillWidth: true
                text: network.busy
                    ? (network.disconnecting ? "Ayarlar geri\nyükleniyor…" : "Tünel\nhazırlanıyor…")
                    : (network.connected ? "Koruma\naktif." : "Bağlanmaya\nhazır.")
                font.pixelSize: 38
                font.weight: Font.Medium
                font.letterSpacing: -1.4
                lineHeight: 1.02
                color: root.theme.ink

                Behavior on text {
                    enabled: root.motion
                    SequentialAnimation {
                        NumberAnimation { target: heroHeadline; property: "opacity"; to: 0.15; duration: 90; easing.type: Easing.InQuad }
                        PropertyAction {}
                        NumberAnimation { target: heroHeadline; property: "opacity"; to: 1.0; duration: 240; easing.type: Easing.OutCubic }
                    }
                }
            }

            Item { Layout.preferredHeight: 12 }

            BodyText {
                Layout.fillWidth: true
                text: network.heroSubtitle
                font.pixelSize: 13
                color: network.error.length > 0 ? root.theme.vermilion : root.theme.inkSoft
                wrapMode: Text.WordWrap
                lineHeight: 1.4
            }

            Item { Layout.fillHeight: true }

            // Tactile Primary Action Button
            Rectangle {
                id: mainBtn
                Layout.fillWidth: true
                Layout.preferredHeight: 48
                radius: 13
                antialiasing: true
                color: network.connected
                    ? (mainHit.containsMouse ? "#EFEAE0" : root.theme.card)
                    : (mainHit.containsMouse ? "#2B2722" : root.theme.ink)
                border.color: network.connected ? root.theme.outerBorder : root.theme.ink
                border.width: 1
                scale: mainHit.pressed ? 0.978 : 1.0
                Behavior on color { ColorAnimation { duration: 180; easing.type: Easing.OutCubic } }
                Behavior on scale { SpringAnimation { spring: 6.5; damping: 0.55 } }

                Row {
                    anchors.centerIn: parent
                    spacing: 10

                    Item {
                        visible: network.busy
                        width: 15; height: 15
                        anchors.verticalCenter: parent.verticalCenter
                        rotation: root.spinAngle
                        Shape {
                            anchors.fill: parent
                            preferredRendererType: Shape.CurveRenderer
                            antialiasing: true
                            ShapePath {
                                strokeColor: network.connected ? root.theme.ink : "#FDFCFA"
                                strokeWidth: 1.8
                                capStyle: ShapePath.RoundCap
                                fillColor: "transparent"
                                PathAngleArc {
                                    centerX: 7.5; centerY: 7.5
                                    radiusX: 6; radiusY: 6
                                    startAngle: 0; sweepAngle: 250
                                }
                            }
                        }
                    }

                    Rectangle {
                        visible: network.connected && !network.busy
                        width: 8; height: 8; radius: 4; antialiasing: true
                        anchors.verticalCenter: parent.verticalCenter
                        color: root.theme.vermilion
                    }
                    BodyText {
                        text: network.actionText
                        font.pixelSize: 14
                        font.weight: Font.Medium
                        color: network.connected ? root.theme.ink : "#FDFCFA"
                    }
                    BodyText {
                        visible: !network.connected && !network.busy
                        text: "→"
                        font.pixelSize: 14
                        color: "#FDFCFA"
                    }
                }

                PressArea {
                    id: mainHit
                    anchors.fill: parent
                    enabled: !network.busy
                    Accessible.name: network.actionText
                    onInvoke: network.toggle()
                }
            }

            Item { Layout.preferredHeight: 12 }

            // Network Profile Selector with Sliding Porcelain Pill
            Rectangle {
                id: profBar
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                radius: 10
                antialiasing: true
                color: root.theme.recess
                border.color: "#DDD6C8"
                border.width: 1

                readonly property real segW: (width - 6) / 4

                Rectangle {
                    x: 3 + root.modeIndex * profBar.segW
                    y: 3
                    width: profBar.segW
                    height: profBar.height - 6
                    radius: 7
                    antialiasing: true
                    color: root.theme.card
                    border.color: "#E2DBD0"
                    border.width: 1
                    Behavior on x { NumberAnimation { duration: 280; easing.type: Easing.OutExpo } }
                }

                Row {
                    anchors.fill: parent
                    anchors.margins: 3
                    Repeater {
                        model: [
                            { id: "auto", label: "Oto" },
                            { id: "strict", label: "Tünel" },
                            { id: "fast", label: "TCP" },
                            { id: "compat", label: "TLS" }
                        ]
                        delegate: Item {
                            required property var modelData
                            readonly property bool sel: network.mode === modelData.id
                            width: profBar.segW
                            height: profBar.height - 6

                            BodyText {
                                anchors.centerIn: parent
                                text: modelData.label
                                font.pixelSize: 12
                                font.weight: sel ? Font.Medium : Font.Normal
                                color: sel ? root.theme.ink : root.theme.inkSoft
                                Behavior on color { ColorAnimation { duration: 150 } }
                            }
                            PressArea {
                                anchors.fill: parent
                                enabled: !network.busy
                                onInvoke: network.setMode(modelData.id)
                            }
                        }
                    }
                }
            }
        }

        // RIGHT COLUMN: Clean Editorial Porcelain Status Card
        Item {
            id: rightCard
            Layout.preferredWidth: 274
            Layout.fillHeight: true
            transform: [
                Scale { id: rightScaleTransform; origin.x: rightCard.width / 2; origin.y: rightCard.height / 2; xScale: 1; yScale: 1 },
                Translate { id: rightShiftTransform; y: 0 }
            ]

            Rectangle {
                x: 0; y: 8; width: parent.width; height: parent.height
                radius: 18; antialiasing: true; color: "#161412"; opacity: 0.045
            }
            Rectangle {
                x: 0; y: 3; width: parent.width; height: parent.height
                radius: 18; antialiasing: true; color: "#161412"; opacity: 0.035
            }

            Rectangle {
                anchors.fill: parent
                radius: 18
                antialiasing: true
                color: root.theme.card
                border.color: network.connected ? "#EAC8BE" : root.theme.hairline
                border.width: 1
                clip: true
                Behavior on border.color { ColorAnimation { duration: 300 } }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 16
                    spacing: 0

                    RowLayout {
                        Layout.fillWidth: true
                        MonoText { text: network.proxyEndpoint; color: root.theme.inkSoft }
                        Item { Layout.fillWidth: true }
                        MonoText {
                            text: network.dohEnabled ? "DoH" : "DNS"
                            font.pixelSize: 10
                            font.weight: Font.DemiBold
                            color: network.dohEnabled ? root.theme.vermilion : root.theme.inkMuted
                        }
                    }

                    Item {
                        Layout.fillWidth: true
                        Layout.fillHeight: true

                        Rectangle {
                            anchors.centerIn: parent
                            visible: root.connectRipple > 0 && root.connectRipple < 1
                            width: 24 + root.connectRipple * 165
                            height: width; radius: width / 2
                            antialiasing: true
                            color: "transparent"
                            border.color: root.theme.vermilion
                            border.width: 1.5
                            opacity: (1 - root.connectRipple) * 0.45
                        }

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.topMargin: 10
                            anchors.bottomMargin: 8
                            spacing: 10

                            RowLayout {
                                Layout.fillWidth: true
                                Column {
                                    spacing: 2
                                    BodyText {
                                        text: network.busy ? "BAĞLANIYOR" : (network.connected ? "ANLIK İNDİRME" : "BEKLEMEDE")
                                        font.pixelSize: 9
                                        font.weight: Font.DemiBold
                                        font.letterSpacing: 1.1
                                        color: network.connected ? root.theme.vermilion : root.theme.inkMuted
                                    }
                                    DisplayText {
                                        text: network.connected ? network.downloadSpeed : "0 B/s"
                                        font.pixelSize: 26
                                        font.weight: Font.Medium
                                        font.letterSpacing: -0.8
                                        color: network.connected ? root.theme.ink : root.theme.inkMuted
                                    }
                                }
                                Item { Layout.fillWidth: true }
                                Column {
                                    visible: network.connected
                                    spacing: 2
                                    BodyText {
                                        anchors.right: parent.right
                                        text: "TEPE"
                                        font.pixelSize: 9
                                        font.weight: Font.DemiBold
                                        font.letterSpacing: 1.0
                                        color: root.theme.inkMuted
                                    }
                                    MonoText {
                                        anchors.right: parent.right
                                        text: network.peakSpeed
                                        color: root.theme.inkSoft
                                    }
                                }
                            }

                            Column {
                                Layout.fillWidth: true
                                spacing: 0

                                Repeater {
                                    model: [
                                        { k: "Profil", v: network.modeLabel, accent: false },
                                        { k: "DNS", v: network.dohEnabled ? "DoH · 1.1.1.1" : "Sistem DNS", accent: network.dohEnabled && network.connected },
                                        { k: "Kapsam", v: network.systemProxy ? "Tam Sistem + PAC" : "Sadece PAC", accent: false },
                                        { k: "Tünel", v: network.tunnelLabel, accent: network.connected && network.tunnelCount > 0 }
                                    ]
                                    delegate: Item {
                                        required property var modelData
                                        width: parent.width
                                        height: 27

                                        Rectangle {
                                            anchors.top: parent.top
                                            width: parent.width; height: 1
                                            color: "#0D161412"
                                        }

                                        RowLayout {
                                            anchors.fill: parent
                                            BodyText { text: modelData.k; font.pixelSize: 11; color: root.theme.inkMuted }
                                            Item { Layout.fillWidth: true }
                                            MonoText {
                                                text: modelData.v
                                                font.weight: modelData.accent ? Font.DemiBold : Font.Normal
                                                color: modelData.accent ? root.theme.vermilion : root.theme.inkSoft
                                            }
                                        }
                                    }
                                }
                            }

                            Item { Layout.fillHeight: true }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: root.theme.hairline }
                    Item { Layout.preferredHeight: 12 }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        rowSpacing: 10
                        columnSpacing: 12

                        Column {
                            Layout.fillWidth: true; spacing: 2
                            BodyText { text: "TOPLAM"; font.pixelSize: 9; font.weight: Font.DemiBold; font.letterSpacing: 1.0; color: root.theme.inkMuted }
                            MonoText { text: network.totalTraffic; font.pixelSize: 13; font.weight: Font.Medium; color: root.theme.ink }
                        }
                        Column {
                            Layout.fillWidth: true; spacing: 2
                            BodyText { text: "SÜRE"; font.pixelSize: 9; font.weight: Font.DemiBold; font.letterSpacing: 1.0; color: root.theme.inkMuted }
                            MonoText { text: network.uptimeText; font.pixelSize: 13; font.weight: Font.Medium; color: root.theme.ink }
                        }
                        Column {
                            Layout.fillWidth: true; spacing: 2
                            BodyText { text: "OTURUM"; font.pixelSize: 9; font.weight: Font.DemiBold; font.letterSpacing: 1.0; color: root.theme.inkMuted }
                            MonoText { text: network.activeConnections + " aktif · " + network.totalConnections; color: root.theme.inkSoft }
                        }
                        Column {
                            Layout.fillWidth: true; spacing: 2
                            BodyText { text: "DPI · DOH"; font.pixelSize: 9; font.weight: Font.DemiBold; font.letterSpacing: 1.0; color: root.theme.inkMuted }
                            MonoText { text: (network.tcpFragCount + network.tlsRecordCount) + " · " + network.dohHits; color: root.theme.inkSoft }
                        }
                    }
                }
            }
        }
    }
}
