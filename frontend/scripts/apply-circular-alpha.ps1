param(
    [Parameter(Mandatory = $true)]
    [string]$Source,
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [double]$InsetRatio = 0.105
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$resolvedSource = (Resolve-Path $Source).Path
$sourceBitmap = [System.Drawing.Bitmap]::FromFile($resolvedSource)

try {
    if ($sourceBitmap.Width -ne $sourceBitmap.Height) {
        throw "Icon source must be square: $resolvedSource"
    }

    $size = $sourceBitmap.Width
    $inset = [single][Math]::Round($size * $InsetRatio)
    $diameter = [single]($size - (2 * $inset))
    if ($diameter -le 0) {
        throw "Inset ratio leaves no drawable icon area."
    }

    $output = New-Object System.Drawing.Bitmap(
        $size,
        $size,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
    )
    try {
        $graphics = [System.Drawing.Graphics]::FromImage($output)
        try {
            $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
            $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
            $graphics.Clear([System.Drawing.Color]::Transparent)

            $brush = New-Object System.Drawing.TextureBrush(
                $sourceBitmap,
                [System.Drawing.Drawing2D.WrapMode]::Clamp
            )
            try {
                $bounds = New-Object System.Drawing.RectangleF(
                    $inset,
                    $inset,
                    $diameter,
                    $diameter
                )
                $graphics.FillEllipse($brush, $bounds)
            }
            finally {
                $brush.Dispose()
            }
        }
        finally {
            $graphics.Dispose()
        }

        $destinationDirectory = Split-Path -Parent $Destination
        if ($destinationDirectory) {
            New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
        }
        $output.Save(
            $Destination,
            [System.Drawing.Imaging.ImageFormat]::Png
        )

        $cornerAlpha = $output.GetPixel(0, 0).A
        $centerAlpha = $output.GetPixel(
            [int]($size / 2),
            [int]($size / 2)
        ).A
        if ($cornerAlpha -ne 0 -or $centerAlpha -ne 255) {
            throw "Generated icon failed alpha validation (corner=$cornerAlpha, center=$centerAlpha)."
        }
    }
    finally {
        $output.Dispose()
    }
}
finally {
    $sourceBitmap.Dispose()
}

Write-Host "Transparent icon ready: $Destination"
