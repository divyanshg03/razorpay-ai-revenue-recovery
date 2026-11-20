<#
Deploy a committed revision to Uprush. Never the working folder.

    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1            # deploys HEAD
    powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Ref main

Why an export and not `uprush deploy` in the repo: the repo root holds .env (Razorpay test
keys) and rzp-key.csv, and the CLI uploads the folder it runs in. `git archive` contains only
committed files, and both of those are gitignored, so neither can be in what goes up.

Why --no-env: the CLI otherwise sends .env values the project lacks. The receiver needs one
variable, RAZORPAY_WEBHOOK_SECRET, set once with
    uprush env push .env --only RAZORPAY_WEBHOOK_SECRET --service receiver --project razorpay-revenue-recovery --yes
and the project's `env auto` is off, so the API keys never leave this machine.

Structure (which folder is which part, and the routes) is deploy\uprush.json.
#>
param(
    [string]$Ref = "HEAD",
    [string]$Project = "razorpay-revenue-recovery"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

# A stale results page would publish figures the README no longer stands behind.
& python (Join-Path $repo "scripts\render_site.py") --check
if ($LASTEXITCODE -ne 0) { throw "site/index.html is stale - run scripts/render_site.py and commit" }

$sha = (& git -C $repo rev-parse --short $Ref).Trim()
$work = Join-Path ([IO.Path]::GetTempPath()) "uprush-deploy-$sha-$(Get-Random)"
$zip = "$work.zip"
New-Item -ItemType Directory $work | Out-Null
try {
    # A zip rather than a tar piped through PowerShell 5.1, which re-encodes binary pipes.
    & git -C $repo archive --format=zip -o $zip $Ref
    if ($LASTEXITCODE -ne 0) { throw "git archive failed for $Ref" }
    Expand-Archive -Path $zip -DestinationPath $work

    $leaked = Get-ChildItem $work -Recurse -Force -File |
        Where-Object { $_.Name -like ".env*" -or $_.Name -like "rzp-key*" }
    if ($leaked) { throw "refusing to deploy: secret-looking files in the export: $($leaked.FullName)" }

    Push-Location $work
    try {
        Write-Host "deploying $sha to $Project"
        & uprush deploy --json --no-env --wait 90 --project $Project --yes `
            --spec (Join-Path $work "deploy\uprush.json")
        $code = $LASTEXITCODE
        # 6 = still building; follow it to the end rather than reporting it as stuck.
        while ($code -eq 6) {
            & uprush status --json --wait 90 --project $Project
            $code = $LASTEXITCODE
        }
        if ($code -ne 0) { throw "deploy ended with exit code $code" }
    } finally { Pop-Location }
} finally {
    Remove-Item $work, $zip -Recurse -Force -ErrorAction SilentlyContinue
}
