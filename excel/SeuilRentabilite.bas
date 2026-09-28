Attribute VB_Name = "SeuilRentabilite"
'===============================================================================
' Seuil de rentabilité : combinaisons clients Offre simple x Offre premium
'
' Lit les valeurs de la feuille "BusinessModel" puis crée une feuille
' "Rentabilite" contenant :
'   1. Une grille (nb clients simple en colonnes, nb clients premium en lignes)
'      avec le montant total pour chaque combinaison.
'   2. Un graphique SURFACE 3D : axe X = clients simple, axe Y = clients premium,
'      axe Z = montant total. Les bandes de couleur sont calées sur le seuil :
'      rouge = sous le seuil, vert = au-dessus.
'   3. Un graphique 2D "frontière de rentabilité" : pour chaque nombre de
'      clients simple, le nombre de clients premium nécessaire pour atteindre
'      le seuil (valeur exacte + arrondi au client supérieur), avec le seuil
'      affiché en ligne horizontale sur l'axe des montants.
'
' Utilisation : Alt+F11 > Insertion > Module, copier-coller ce code
'               (l'import direct du .bas UTF-8 abîme les accents),
'               puis lancer la macro GenererCourbesRentabilite.
'===============================================================================
Option Explicit

Private Const FEUILLE_SOURCE As String = "BusinessModel"
Private Const FEUILLE_CIBLE As String = "Rentabilite"

' True  : montant = marge (colonne I) -> cohérent avec le seuil calculé ligne 16/21
' False : montant = chiffre d'affaires encaissé (colonne E)
Private Const UTILISER_MARGE As Boolean = True

Public Sub GenererCourbesRentabilite()
    Dim src As Worksheet, ws As Worksheet
    Dim seuil As Double, montantSimple As Double, montantPremium As Double
    Dim maxSimple As Long, maxPremium As Long
    Dim i As Long, j As Long

    Set src = ThisWorkbook.Worksheets(FEUILLE_SOURCE)

    '--- Lecture des paramètres ------------------------------------------------
    seuil = src.Range("E12").Value                         ' coût annuel du service
    If UTILISER_MARGE Then
        montantSimple = src.Range("I14").Value + src.Range("I15").Value
        montantPremium = src.Range("I18").Value + src.Range("I19").Value + src.Range("I20").Value
    Else
        montantSimple = src.Range("E14").Value + src.Range("E15").Value
        montantPremium = src.Range("E18").Value + src.Range("E19").Value + src.Range("E20").Value
    End If

    If montantSimple <= 0 Or montantPremium <= 0 Then
        MsgBox "Montant par client nul ou négatif : vérifiez les cellules I14:I20.", vbCritical
        Exit Sub
    End If

    ' Plage de clients : 2x le seuil "mono-offre" pour bien voir la frontière
    maxSimple = Application.WorksheetFunction.RoundUp(seuil / montantSimple, 0) * 2
    maxPremium = Application.WorksheetFunction.RoundUp(seuil / montantPremium, 0) * 2

    '--- Préparation de la feuille cible ---------------------------------------
    Application.ScreenUpdating = False
    Application.DisplayAlerts = False
    On Error Resume Next
    ThisWorkbook.Worksheets(FEUILLE_CIBLE).Delete
    On Error GoTo 0
    Application.DisplayAlerts = True

    Set ws = ThisWorkbook.Worksheets.Add(After:=src)
    ws.Name = FEUILLE_CIBLE

    '--- Paramètres (formules liées à la feuille source) ----------------------
    ws.Range("A1").Value = "Seuil de rentabilité (€/an)"
    ws.Range("B1").Formula = "='" & FEUILLE_SOURCE & "'!E12"
    ws.Range("A2").Value = IIf(UTILISER_MARGE, "Marge", "CA") & " / client simple"
    ws.Range("A3").Value = IIf(UTILISER_MARGE, "Marge", "CA") & " / client premium"
    If UTILISER_MARGE Then
        ws.Range("B2").Formula = "='" & FEUILLE_SOURCE & "'!I14+'" & FEUILLE_SOURCE & "'!I15"
        ws.Range("B3").Formula = "='" & FEUILLE_SOURCE & "'!I18+'" & FEUILLE_SOURCE & "'!I19+'" & FEUILLE_SOURCE & "'!I20"
    Else
        ws.Range("B2").Formula = "='" & FEUILLE_SOURCE & "'!E14+'" & FEUILLE_SOURCE & "'!E15"
        ws.Range("B3").Formula = "='" & FEUILLE_SOURCE & "'!E18+'" & FEUILLE_SOURCE & "'!E19+'" & FEUILLE_SOURCE & "'!E20"
    End If
    ws.Range("B1:B3").NumberFormat = "# ##0 €"
    ws.Range("A1:A3").Font.Bold = True

    '--- 1. Grille 3D ----------------------------------------------------------
    ' Ligne 6 = en-têtes clients simple, colonne A = clients premium
    Const L0 As Long = 6
    ws.Cells(L0 - 1, 1).Value = "Montant total (lignes = clients premium, colonnes = clients simple)"
    ws.Cells(L0 - 1, 1).Font.Bold = True
    ws.Cells(L0, 1).Value = "Premium \ Simple"
    For j = 0 To maxSimple
        ws.Cells(L0, 2 + j).Value = j
    Next j
    For i = 0 To maxPremium
        ws.Cells(L0 + 1 + i, 1).Value = i
        For j = 0 To maxSimple
            ' Formule : =simple*$B$2 + premium*$B$3 (reste dynamique)
            ws.Cells(L0 + 1 + i, 2 + j).FormulaR1C1 = _
                "=R" & L0 & "C*R2C2+RC1*R3C2"
        Next j
    Next i

    Dim grille As Range
    Set grille = ws.Range(ws.Cells(L0, 1), ws.Cells(L0 + 1 + maxPremium, 2 + maxSimple))
    grille.Rows(1).Font.Bold = True
    grille.Columns(1).Font.Bold = True
    ws.Range(ws.Cells(L0 + 1, 2), ws.Cells(L0 + 1 + maxPremium, 2 + maxSimple)).NumberFormat = "# ##0"

    ' Mise en forme conditionnelle : rouge sous le seuil, vert au-dessus
    With ws.Range(ws.Cells(L0 + 1, 2), ws.Cells(L0 + 1 + maxPremium, 2 + maxSimple))
        .FormatConditions.Delete
        With .FormatConditions.Add(Type:=xlCellValue, Operator:=xlLess, Formula1:="=$B$1")
            .Interior.Color = RGB(248, 203, 173)
        End With
        With .FormatConditions.Add(Type:=xlCellValue, Operator:=xlGreaterEqual, Formula1:="=$B$1")
            .Interior.Color = RGB(198, 239, 206)
        End With
    End With

    '--- 2. Table frontière 2D -------------------------------------------------
    Dim L1 As Long
    L1 = L0 + maxPremium + 4
    ws.Cells(L1 - 1, 1).Value = "Frontière de rentabilité"
    ws.Cells(L1 - 1, 1).Font.Bold = True
    ws.Cells(L1, 1).Value = "Clients simple"
    ws.Cells(L1, 2).Value = "Clients premium nécessaires (exact)"
    ws.Cells(L1, 3).Value = "Clients premium nécessaires (arrondi)"
    ws.Cells(L1, 4).Value = "Montant avec clients simple seuls"
    ws.Cells(L1, 5).Value = "Seuil"
    ws.Range(ws.Cells(L1, 1), ws.Cells(L1, 5)).Font.Bold = True
    For j = 0 To maxSimple
        ws.Cells(L1 + 1 + j, 1).Value = j
        ws.Cells(L1 + 1 + j, 2).FormulaR1C1 = "=MAX(0,(R1C2-RC1*R2C2)/R3C2)"
        ws.Cells(L1 + 1 + j, 3).FormulaR1C1 = "=ROUNDUP(RC2,0)"
        ws.Cells(L1 + 1 + j, 4).FormulaR1C1 = "=RC1*R2C2"
        ws.Cells(L1 + 1 + j, 5).FormulaR1C1 = "=R1C2"
    Next j
    ws.Range(ws.Cells(L1 + 1, 2), ws.Cells(L1 + 1 + maxSimple, 2)).NumberFormat = "0.00"
    ws.Range(ws.Cells(L1 + 1, 4), ws.Cells(L1 + 1 + maxSimple, 5)).NumberFormat = "# ##0 €"
    ws.Columns("A:E").AutoFit

    '--- Graphiques ------------------------------------------------------------
    Dim gauche As Double
    gauche = ws.Cells(1, 2 + maxSimple + 2).Left

    CreerSurface3D ws, grille, seuil, gauche
    CreerFrontiere ws, L1, maxSimple, gauche

    Application.ScreenUpdating = True
    ws.Activate
    ws.Range("A1").Select
    MsgBox "Graphiques générés sur la feuille '" & FEUILLE_CIBLE & "'." & vbCrLf & _
           "Seuil : " & Format(seuil, "# ##0 €") & vbCrLf & _
           "Seuil offre simple seule : " & Format(seuil / montantSimple, "0.00") & " clients" & vbCrLf & _
           "Seuil offre premium seule : " & Format(seuil / montantPremium, "0.00") & " clients", _
           vbInformation
End Sub

'-------------------------------------------------------------------------------
' Surface 3D : bandes de couleur calées sur le seuil (MajorUnit = seuil)
'-------------------------------------------------------------------------------
Private Sub CreerSurface3D(ws As Worksheet, grille As Range, seuil As Double, gauche As Double)
    Dim co As ChartObject, ch As Chart
    Dim k As Long, nbBandes As Long

    Set co = ws.ChartObjects.Add(Left:=gauche, Top:=ws.Range("A1").Top, Width:=720, Height:=450)
    co.Name = "Surface3D"
    Set ch = co.Chart

    ch.ChartType = xlSurface
    ch.SetSourceData Source:=grille, PlotBy:=xlRows   ' séries = clients premium

    ch.HasTitle = True
    ch.ChartTitle.Text = "Montant annuel selon le nb de clients simple / premium" & vbLf & _
                         "(rouge = sous le seuil de " & Format(seuil, "# ##0 €") & ")"

    With ch.Axes(xlCategory)
        .HasTitle = True
        .AxisTitle.Text = "Clients offre simple"
    End With
    With ch.Axes(xlSeriesAxis)
        .HasTitle = True
        .AxisTitle.Text = "Clients offre premium"
    End With
    With ch.Axes(xlValue)
        .HasTitle = True
        .AxisTitle.Text = "Montant (€/an)"
        .MinimumScale = 0
        .MajorUnit = seuil                  ' 1re frontière de bande = seuil
        .TickLabels.NumberFormat = "# ##0 €"
    End With

    ch.Rotation = 30
    ch.Elevation = 20

    ' Couleur des bandes : 1re (0 -> seuil) en rouge, les suivantes en vert
    ch.HasLegend = True
    ch.Legend.Position = xlLegendPositionRight
    On Error Resume Next
    nbBandes = ch.Legend.LegendEntries.Count
    For k = 1 To nbBandes
        With ch.Legend.LegendEntries(k).LegendKey.Format.Fill
            .Visible = msoTrue
            .Solid
            If k = 1 Then
                .ForeColor.RGB = RGB(220, 60, 60)
            Else
                ' dégradé de verts de plus en plus foncés
                .ForeColor.RGB = RGB(200 - 30 * (k - 2), 235 - 15 * (k - 2), 190 - 30 * (k - 2))
            End If
        End With
    Next k
    On Error GoTo 0
End Sub

'-------------------------------------------------------------------------------
' Courbe 2D : nb de clients premium nécessaires en fonction des clients simple
'             + montant des clients simple seuls vs ligne de seuil (axe secondaire)
'-------------------------------------------------------------------------------
Private Sub CreerFrontiere(ws As Worksheet, L1 As Long, maxSimple As Long, gauche As Double)
    Dim co As ChartObject, ch As Chart, s As Series
    Dim xRange As Range

    Set xRange = ws.Range(ws.Cells(L1 + 1, 1), ws.Cells(L1 + 1 + maxSimple, 1))

    Set co = ws.ChartObjects.Add(Left:=gauche, Top:=ws.Range("A1").Top + 470, Width:=720, Height:=400)
    co.Name = "Frontiere"
    Set ch = co.Chart
    ch.ChartType = xlXYScatterLines

    Do While ch.SeriesCollection.Count > 0
        ch.SeriesCollection(1).Delete
    Loop

    ' Frontière exacte
    Set s = ch.SeriesCollection.NewSeries
    s.Name = "Premium nécessaires (exact)"
    s.XValues = xRange
    s.Values = xRange.Offset(0, 1)
    s.ChartType = xlXYScatterLinesNoMarkers
    s.Format.Line.ForeColor.RGB = RGB(47, 85, 151)
    s.Format.Line.Weight = 2.5

    ' Frontière arrondie (clients entiers)
    Set s = ch.SeriesCollection.NewSeries
    s.Name = "Premium nécessaires (arrondi)"
    s.XValues = xRange
    s.Values = xRange.Offset(0, 2)
    s.ChartType = xlXYScatter
    s.MarkerStyle = xlMarkerStyleCircle
    s.MarkerSize = 6
    s.Format.Fill.ForeColor.RGB = RGB(237, 125, 49)

    ' Montant clients simple seuls (axe secondaire)
    Set s = ch.SeriesCollection.NewSeries
    s.Name = "Montant clients simple seuls"
    s.XValues = xRange
    s.Values = xRange.Offset(0, 3)
    s.ChartType = xlXYScatterLinesNoMarkers
    s.AxisGroup = xlSecondary
    s.Format.Line.ForeColor.RGB = RGB(112, 173, 71)
    s.Format.Line.DashStyle = msoLineDash

    ' Seuil (axe secondaire, ligne horizontale)
    Set s = ch.SeriesCollection.NewSeries
    s.Name = "Seuil de rentabilité"
    s.XValues = xRange
    s.Values = xRange.Offset(0, 4)
    s.ChartType = xlXYScatterLinesNoMarkers
    s.AxisGroup = xlSecondary
    s.Format.Line.ForeColor.RGB = RGB(192, 0, 0)
    s.Format.Line.Weight = 2

    ch.HasTitle = True
    ch.ChartTitle.Text = "Frontière de rentabilité : clients premium nécessaires selon les clients simple"

    With ch.Axes(xlCategory, xlPrimary)
        .HasTitle = True
        .AxisTitle.Text = "Clients offre simple"
        .MinimumScale = 0
        .MaximumScale = maxSimple
        .MajorUnit = 2
    End With
    With ch.Axes(xlValue, xlPrimary)
        .HasTitle = True
        .AxisTitle.Text = "Clients offre premium nécessaires"
        .MinimumScale = 0
    End With
    With ch.Axes(xlValue, xlSecondary)
        .HasTitle = True
        .AxisTitle.Text = "Montant (€/an)"
        .MinimumScale = 0
        .TickLabels.NumberFormat = "# ##0 €"
    End With

    ch.HasLegend = True
    ch.Legend.Position = xlLegendPositionBottom
End Sub
