param([Parameter(Mandatory = $true)][string]$RequestPath)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$missing = [Type]::Missing

function Open-Workbook($Excel, [string]$Path) {
    # UpdateLinks=0, ReadOnly=true, empty passwords, IgnoreReadOnlyRecommended=true.
    return $Excel.Workbooks.Open($Path, 0, $true, $missing, "", "", $true)
}

function Get-WorkbookSnapshot($Book) {
    $sheets = foreach ($sheet in $Book.Sheets) {
        [pscustomobject]@{ name = $sheet.Name; visibility = $sheet.Visible; type = $sheet.Type }
    }
    $names = foreach ($name in $Book.Names) {
        [pscustomobject]@{ name = $name.Name; formula = $name.RefersTo; visible = $name.Visible }
    }
    $worksheets = foreach ($sheet in $Book.Worksheets) {
        $used = $sheet.UsedRange
        if ($used.CountLarge -gt 250000) { throw "A worksheet exceeds the 250000-cell verification limit." }
        $rows = $used.Rows.Count
        $columns = $used.Columns.Count
        $firstRow = $used.Row
        $firstColumn = $used.Column
        $formula = $used.Formula
        $cells = [Collections.Generic.List[object]]::new()
        for ($row = 1; $row -le $rows; $row++) {
            for ($column = 1; $column -le $columns; $column++) {
                $value = if ($formula -is [array]) { $formula.GetValue($row, $column) } else { $formula }
                if ($null -ne $value -and !($value -is [string] -and $value -ceq "")) {
                    $cells.Add(@(($firstRow + $row - 1), ($firstColumn + $column - 1), $value))
                }
            }
        }
        $validationCells = $null
        try {
            $validationCells = $sheet.Cells.SpecialCells(-4174)  # xlCellTypeAllValidation
        } catch [Runtime.InteropServices.COMException] {
            if ($_.Exception.HResult -ne -2146827284) { throw }
        }
        $validations = foreach ($area in $validationCells.Areas) {
            if ($area.CountLarge -gt 4096) { throw "A validation range exceeds the 4096-cell verification limit." }
            foreach ($cell in $area.Cells) {
                $rule = $cell.Validation
                # Formula2 and Operator are only applicable to numeric/date comparisons.
                $operator = if ($rule.Type -in 1, 2, 4, 5, 6) { $rule.Operator }
                $second = if ($operator -in 1, 2) { $rule.Formula2 }
                $dropdown = if ($rule.Type -eq 3) { $rule.InCellDropdown }
                [pscustomobject]@{
                    cell = $cell.Address(); type = $rule.Type; operator = $operator; first = $rule.Formula1
                    second = $second; dropdown = $dropdown; allow_blank = $rule.IgnoreBlank
                    alert = $rule.AlertStyle; show_error = $rule.ShowError; error_title = $rule.ErrorTitle
                    error_message = $rule.ErrorMessage; show_input = $rule.ShowInput
                    input_title = $rule.InputTitle; input_message = $rule.InputMessage
                }
            }
        }
        [pscustomobject]@{ sheet = $sheet.Name; cells = @($cells.ToArray()); validation = @($validations) }
    }
    # Ignore the used-range bounds: applying style to blank cells can extend them.
    return ([pscustomobject]@{
        format = $Book.FileFormat; sheets = @($sheets); names = @($names); data = @($worksheets)
    } | ConvertTo-Json -Depth 8 -Compress)
}

function Get-WorkbookLayout($Book) {
    foreach ($sheet in $Book.Worksheets) {
        $used = $sheet.UsedRange
        $preview = $sheet.Range($sheet.Cells.Item($used.Row, $used.Column),
            $sheet.Cells.Item(($used.Row + [Math]::Min($used.Rows.Count, 14) - 1),
                ($used.Column + [Math]::Min($used.Columns.Count, 12) - 1)))
        $previewValues = $preview.Value2
        $labels = for ($row = 1; $row -le $preview.Rows.Count; $row++) {
            for ($column = 1; $column -le $preview.Columns.Count; $column++) {
                $value = if ($previewValues -is [array]) { $previewValues.GetValue($row, $column) } else { $previewValues }
                if ($value -is [string] -and $value.Trim()) {
                    $cell = $preview.Cells.Item($row, $column)
                    [pscustomobject]@{ cell = $cell.Address($false, $false); text = $value.Substring(0, [Math]::Min(160, $value.Length)) }
                }
            }
        }
        [pscustomobject]@{ sheet = $sheet.Name; used_range = $used.Address($false, $false); labels = @($labels) }
    }
}

function Get-ExactSheet($Book, [string]$Name) {
    $sheet = $Book.Worksheets.Item($Name)
    if ($sheet.Name -cne $Name) { throw "Select the worksheet by its exact name." }
    return $sheet
}

function Copy-Properties($From, $To, [string[]]$Names) {
    foreach ($name in $Names) {
        $value = $From.$name
        if ($null -ne $value) { $To.$name = $value }
    }
}

function Copy-CellFormat($From, $To) {
    Copy-Properties $From.Font $To.Font @("Name", "Size", "Bold", "Italic", "Underline", "Strikethrough", "Color")
    Copy-Properties $From $To @("HorizontalAlignment", "VerticalAlignment", "Orientation", "WrapText", "ShrinkToFit", "IndentLevel")
    $fill = $From.Interior
    if ($fill.Pattern -ne -4142) {  # xlPatternNone
        $To.Interior.Color = $fill.Color
        $To.Interior.PatternColor = $fill.PatternColor
    }
    $To.Interior.Pattern = $fill.Pattern
    foreach ($edge in 5..10) {
        $border = $From.Borders.Item($edge)
        $targetBorder = $To.Borders.Item($edge)
        if ($border.LineStyle -eq -4142) {
            $targetBorder.LineStyle = -4142  # xlLineStyleNone
        } else {
            Copy-Properties $border $targetBorder @("Weight", "LineStyle", "Color")
        }
    }
}

function Apply-FormatMap($Source, $Reference, $Mappings) {
    foreach ($map in $Mappings) {
        $sample = (Get-ExactSheet $Reference $map.reference_sheet).Range($map.reference_range)
        $target = (Get-ExactSheet $Source $map.target_sheet).Range($map.target_range)
        $sampleRows = $sample.Rows.Count
        $sampleColumns = $sample.Columns.Count
        $targetRows = $target.Rows.Count
        $targetColumns = $target.Columns.Count
        for ($row = 1; $row -le $targetRows; $row++) {
            $sampleRow = if ($sampleRows -eq 1) { 1 } else { $row }
            if ($map.row_heights -and !$target.Rows.Item($row).EntireRow.Hidden) {
                $target.Rows.Item($row).RowHeight = $sample.Rows.Item($sampleRow).RowHeight
            }
            for ($column = 1; $column -le $targetColumns; $column++) {
                $sampleColumn = if ($sampleColumns -eq 1) { 1 } else { $column }
                if ($row -eq 1 -and $map.column_widths -and !$target.Columns.Item($column).EntireColumn.Hidden) {
                    $target.Columns.Item($column).ColumnWidth = $sample.Columns.Item($sampleColumn).ColumnWidth
                }
                Copy-CellFormat $sample.Cells.Item($sampleRow, $sampleColumn) $target.Cells.Item($row, $column)
            }
        }
    }
}

$excel = $source = $reference = $reopened = $null
$code = "WORKBOOK_EXCEL_REQUIRED"
$status = 0
try {
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $excel = New-Object -ComObject Excel.Application
    $excel.DisplayAlerts = $false
    $excel.EnableEvents = $false
    $excel.AskToUpdateLinks = $false
    $excel.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
    $code = "WORKBOOK_EXCEL_FAILED"
    $source = Open-Workbook $excel $request.source
    if ($request.action -eq "inspect") {
        $result = @{ state = "INSPECTED"; format = $source.FileFormat; sheets = @(Get-WorkbookLayout $source) }
    } elseif ($request.action -eq "write") {
        $reference = Open-Workbook $excel $request.reference
        $before = Get-WorkbookSnapshot $source
        Apply-FormatMap $source $reference $request.mappings
        $source.SaveCopyAs($request.output)
        $source.Close($false)
        $source = $null
        $reopened = Open-Workbook $excel $request.output
        $code = "WORKBOOK_VERIFICATION_FAILED"
        if ((Get-WorkbookSnapshot $reopened) -cne $before) {
            throw "The saved copy changed workbook content, worksheet structure, named ranges or validation rules."
        }
        $result = @{ state = "VERIFIED"; native_format = $reopened.FileFormat; preservation = "PASSED"; layout_review = "UNVERIFIED" }
    } else {
        throw "Unsupported Excel adapter action."
    }
} catch {
    $result = @{ state = "BLOCKED"; code = $code; message = $_.Exception.Message; adapter_line = $_.InvocationInfo.ScriptLineNumber }
    $status = 2
} finally {
    foreach ($book in @($reopened, $reference, $source)) {
        if ($book) {
            try { $book.Close($false) } catch {}
        }
    }
    if ($excel) {
        try { $excel.Quit() } catch {}
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($excel)
    }
}
$result | ConvertTo-Json -Depth 8 -Compress
exit $status
