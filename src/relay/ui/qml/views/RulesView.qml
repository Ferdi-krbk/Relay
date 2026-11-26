import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

ScrollView {
    id: root
    required property var theme
    clip: true
    contentWidth: availableWidth

    ColumnLayout {
        width: parent.width
        spacing: 12

        Item { Layout.preferredHeight: 6 }

        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: 26
            Layout.rightMargin: 26
            implicitHeight: togglesCol.implicitHeight + 32
            radius: 15
            antialiasing: true
            color: root.theme.card
            border.color: root.theme.hairline

            Column {
                id: togglesCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 16
                spacing: 12

                RowLayout {
                    width: parent.width
                    spacing: 12
                    Column {
                        Layout.fillWidth: true; spacing: 1
                        BodyText { width: parent.width; text: "DNS-over-HTTPS (DoH)"; font.weight: Font.Medium; color: root.theme.ink }
                        BodyText { width: parent.width; wrapMode: Text.WordWrap; text: "Engelli sitelerde ISS DNS engellerini aşmak için 1.1.1.1 / 8.8.8.8 kullanır."; font.pixelSize: 11; color: root.theme.inkMuted }
                    }
                    CeramicSwitch { checked: network.dohEnabled; onToggled: function(v) { network.setDohEnabled(v) } }
                }
                Rectangle { width: parent.width; height: 1; color: root.theme.hairline }
                RowLayout {
                    width: parent.width
                    spacing: 12
                    Column {
                        Layout.fillWidth: true; spacing: 1
                        BodyText { width: parent.width; text: "Tam Sistem Proxy Modu"; font.weight: Font.Medium; color: root.theme.ink }
                        BodyText { width: parent.width; wrapMode: Text.WordWrap; text: "PAC kullanmayan masaüstü uygulamaları (Discord / Roblox / Steam) için ProxyServer'ı da açar."; font.pixelSize: 11; color: root.theme.inkMuted }
                    }
                    CeramicSwitch { checked: network.systemProxy; onToggled: function(v) { network.setSystemProxy(v) } }
                }
                Rectangle { width: parent.width; height: 1; color: root.theme.hairline }
                RowLayout {
                    width: parent.width
                    spacing: 12
                    Column {
                        Layout.fillWidth: true; spacing: 1
                        BodyText { width: parent.width; text: "Oyun / Uygulama Modu (Split-TUN)"; font.weight: Font.Medium; color: root.theme.ink }
                        BodyText { width: parent.width; wrapMode: Text.WordWrap; text: "Sadece aşağıdaki listedeki oyun/uygulamaları (Roblox vb.) MASQUE tüneline yönlendirir; diğer trafik direkt kalır. Yönetici izni ister."; font.pixelSize: 11; color: root.theme.inkMuted }
                    }
                    CeramicSwitch { checked: network.gameModeActive; onToggled: function(v) { network.setGameMode(v) } }
                }
                Rectangle { width: parent.width; height: 1; color: root.theme.hairline }
                RowLayout {
                    width: parent.width
                    spacing: 12
                    Column {
                        Layout.fillWidth: true; spacing: 1
                        BodyText { width: parent.width; text: "Hareket Efektleri"; font.weight: Font.Medium; color: root.theme.ink }
                        BodyText { width: parent.width; wrapMode: Text.WordWrap; text: "Açılış/kapanış koreografisi ve yumuşak sekme geçişleri."; font.pixelSize: 11; color: root.theme.inkMuted }
                    }
                    CeramicSwitch { checked: network.animations; onToggled: function(v) { network.setAnimations(v) } }
                }
                Rectangle { width: parent.width; height: 1; color: root.theme.hairline }
                RowLayout {
                    width: parent.width
                    spacing: 12
                    Column {
                        Layout.fillWidth: true; spacing: 1
                        BodyText { width: parent.width; text: "Windows ile Tepside Başlat"; font.weight: Font.Medium; color: root.theme.ink }
                        BodyText { width: parent.width; wrapMode: Text.WordWrap; text: "Başlangıç klasörüne güvenli kısayol ekler; açılışta sessizce tepside başlar."; font.pixelSize: 11; color: root.theme.inkMuted }
                    }
                    CeramicSwitch { checked: network.startWithWindows; onToggled: function(v) { network.setStartWithWindows(v) } }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 26
            Layout.rightMargin: 26
            spacing: 12

            // Force-DPI Domains
            Rectangle {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                implicitHeight: fCol.implicitHeight + 28
                radius: 15
                antialiasing: true
                color: root.theme.card
                border.color: root.theme.hairline

                ColumnLayout {
                    id: fCol
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    anchors.margins: 14
                    spacing: 8

                    BodyText { text: "Öncelikli DPI Kırma"; font.pixelSize: 12; font.weight: Font.Medium; color: root.theme.ink }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 6
                        TextField {
                            id: fIn
                            Layout.fillWidth: true; height: 30
                            placeholderText: "discord.com"
                            color: root.theme.ink
                            placeholderTextColor: root.theme.inkMuted
                            font.pixelSize: 11
                            background: Rectangle {
                                radius: 7; antialiasing: true
                                color: root.theme.canvas
                                border.color: fIn.activeFocus ? root.theme.ink : root.theme.hairline
                            }
                            onAccepted: { network.addForceDomain(text); text = "" }
                        }
                        Rectangle {
                            width: 48; height: 30; radius: 7; antialiasing: true; color: root.theme.ink
                            BodyText { anchors.centerIn: parent; text: "Ekle"; font.pixelSize: 11; color: "#FDFCFA" }
                            PressArea { anchors.fill: parent; onInvoke: { network.addForceDomain(fIn.text); fIn.text = "" } }
                        }
                    }
                    Flow {
                        Layout.fillWidth: true; spacing: 5
                        Repeater {
                            model: network.forceDomains
                            delegate: Rectangle {
                                required property string modelData
                                height: 24; width: fcT.implicitWidth + 24; radius: 6; antialiasing: true
                                color: root.theme.vermilionSoft
                                border.color: "#F3CFC5"
                                Row {
                                    anchors.centerIn: parent; spacing: 6
                                    MonoText { id: fcT; text: modelData; font.pixelSize: 10; color: root.theme.vermilion }
                                    BodyText { text: "×"; font.pixelSize: 11; color: root.theme.vermilion }
                                }
                                PressArea { anchors.fill: parent; onInvoke: network.removeForceDomain(modelData) }
                            }
                        }
                    }
                }
            }

            // Direct Bypass Domains
            Rectangle {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                implicitHeight: bCol.implicitHeight + 28
                radius: 15
                antialiasing: true
                color: root.theme.card
                border.color: root.theme.hairline

                ColumnLayout {
                    id: bCol
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    anchors.margins: 14
                    spacing: 8

                    BodyText { text: "Direkt Geçiş (Bypass)"; font.pixelSize: 12; font.weight: Font.Medium; color: root.theme.ink }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 6
                        TextField {
                            id: bIn
                            Layout.fillWidth: true; height: 30
                            placeholderText: "banka.com.tr"
                            color: root.theme.ink
                            placeholderTextColor: root.theme.inkMuted
                            font.pixelSize: 11
                            background: Rectangle {
                                radius: 7; antialiasing: true
                                color: root.theme.canvas
                                border.color: bIn.activeFocus ? root.theme.ink : root.theme.hairline
                            }
                            onAccepted: { network.addBypassDomain(text); text = "" }
                        }
                        Rectangle {
                            width: 48; height: 30; radius: 7; antialiasing: true; color: root.theme.ink
                            BodyText { anchors.centerIn: parent; text: "Ekle"; font.pixelSize: 11; color: "#FDFCFA" }
                            PressArea { anchors.fill: parent; onInvoke: { network.addBypassDomain(bIn.text); bIn.text = "" } }
                        }
                    }
                    Flow {
                        Layout.fillWidth: true; spacing: 5
                        Repeater {
                            model: network.bypassDomains
                            delegate: Rectangle {
                                required property string modelData
                                height: 24; width: bcT.implicitWidth + 24; radius: 6; antialiasing: true
                                color: root.theme.recess
                                border.color: root.theme.outerBorder
                                Row {
                                    anchors.centerIn: parent; spacing: 6
                                    MonoText { id: bcT; text: modelData; font.pixelSize: 10; color: root.theme.inkSoft }
                                    BodyText { text: "×"; font.pixelSize: 11; color: root.theme.inkMuted }
                                }
                                PressArea { anchors.fill: parent; onInvoke: network.removeBypassDomain(modelData) }
                            }
                        }
                    }
                }
            }
        }

        // Game Mode Split-Tunnel Apps
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: 26
            Layout.rightMargin: 26
            implicitHeight: gCol.implicitHeight + 28
            radius: 15
            antialiasing: true
            color: root.theme.card
            border.color: network.gameModeActive ? "#EAC8BE" : root.theme.hairline

            ColumnLayout {
                id: gCol
                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                anchors.margins: 14
                spacing: 8

                BodyText {
                    text: network.gameModeActive ? "Tünel Uygulamaları (Oyun Modu Aktif)" : "Tünel Uygulamaları (.exe)"
                    font.pixelSize: 12; font.weight: Font.Medium
                    color: network.gameModeActive ? root.theme.vermilion : root.theme.ink
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 6
                    TextField {
                        id: gIn
                        Layout.fillWidth: true; height: 30
                        placeholderText: "RobloxPlayerBeta.exe"
                        color: root.theme.ink
                        placeholderTextColor: root.theme.inkMuted
                        font.pixelSize: 11
                        background: Rectangle {
                            radius: 7; antialiasing: true
                            color: root.theme.canvas
                            border.color: gIn.activeFocus ? root.theme.ink : root.theme.hairline
                        }
                        onAccepted: { network.addGameApp(text); text = "" }
                    }
                    Rectangle {
                        width: 54; height: 30; radius: 7; antialiasing: true
                        color: root.theme.recess
                        border.color: root.theme.hairline
                        BodyText { anchors.centerIn: parent; text: "Seç…"; font.pixelSize: 11; color: root.theme.ink }
                        PressArea { anchors.fill: parent; onInvoke: network.browseGameApp() }
                    }
                    Rectangle {
                        width: 48; height: 30; radius: 7; antialiasing: true; color: root.theme.ink
                        BodyText { anchors.centerIn: parent; text: "Ekle"; font.pixelSize: 11; color: "#FDFCFA" }
                        PressArea { anchors.fill: parent; onInvoke: { network.addGameApp(gIn.text); gIn.text = "" } }
                    }
                }
                Flow {
                    Layout.fillWidth: true; spacing: 5
                    Repeater {
                        model: network.gameApps
                        delegate: Rectangle {
                            required property string modelData
                            height: 24; width: gcT.implicitWidth + 24; radius: 6; antialiasing: true
                            color: root.theme.forestSoft
                            border.color: "#C5E3D4"
                            Row {
                                anchors.centerIn: parent; spacing: 6
                                MonoText { id: gcT; text: modelData; font.pixelSize: 10; color: root.theme.forest }
                                BodyText { text: "×"; font.pixelSize: 11; color: root.theme.forest }
                            }
                            PressArea { anchors.fill: parent; onInvoke: network.removeGameApp(modelData) }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    BodyText { text: "Açık Uygulamalardan Ekle"; font.pixelSize: 11; color: root.theme.inkMuted }
                    Item { Layout.fillWidth: true }
                    Rectangle {
                        width: 64; height: 22; radius: 6; antialiasing: true
                        color: root.theme.recess; border.color: root.theme.hairline
                        BodyText { anchors.centerIn: parent; text: "↻ Tara"; font.pixelSize: 10; color: root.theme.inkSoft }
                        PressArea { anchors.fill: parent; onInvoke: network.refreshRunningApps() }
                    }
                }
                Flow {
                    Layout.fillWidth: true; spacing: 5
                    visible: network.runningApps.length > 0
                    Component.onCompleted: network.refreshRunningApps()
                    Repeater {
                        model: network.runningApps
                        delegate: Rectangle {
                            required property string modelData
                            height: 24; width: rcT.implicitWidth + 24; radius: 6; antialiasing: true
                            color: root.theme.canvas
                            border.color: root.theme.hairline
                            Row {
                                anchors.centerIn: parent; spacing: 5
                                BodyText { text: "+"; font.pixelSize: 11; color: root.theme.inkSoft }
                                MonoText { id: rcT; text: modelData; font.pixelSize: 10; color: root.theme.inkSoft }
                            }
                            PressArea { anchors.fill: parent; onInvoke: network.addGameApp(modelData) }
                        }
                    }
                }
            }
        }

        Item { Layout.preferredHeight: 14 }
    }
}
