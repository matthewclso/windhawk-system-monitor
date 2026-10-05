# SPDX-License-Identifier: GPL-3.0-only
# All paths/XML are data from stdin. Mutations require our marker and action;
# no passwords, elevated principal, or unrelated tasks are used.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$name = 'NotificationCenterMetrics-' + $sid
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('\')
$task = $null
foreach ($candidate in $folder.GetTasks(1)) {
    if ($candidate.Name -eq $name) { $task = $candidate; break }
}
$owned = $false
if ($task) {
    $definition = $task.Definition
    if ($definition.Actions.Count -eq 1) {
        $action = $definition.Actions.Item(1)
        $owned = ($definition.RegistrationInfo.Description -eq $request.marker) -and
                 ($action.Arguments.Trim('"') -eq $request.collector)
    }
}
if ($request.operation -ne 'status' -and $task -and -not $owned) {
    throw 'Refusing to modify an unrelated task with this name.'
}
switch ($request.operation) {
    'status' {}
    'register' {
        # TASK_CREATE_OR_UPDATE=6, TASK_LOGON_INTERACTIVE_TOKEN=3: logged-in user
        # credentials and DPAPI remain available, without storing a password.
        $task = $folder.RegisterTask($name, $request.xml, 6, $sid, $null, 3)
        $owned = $true
    }
    'disable' { if ($task) { $task.Enabled = $false } }
    'start' { if (-not $task) { throw 'Task is not registered.' }; $null = $task.Run($null) }
    'stop' { if ($task) { $task.Stop(0) } }
    'remove' { if ($task) { $folder.DeleteTask($name, 0); $task = $null } }
    default { throw 'Unsupported operation.' }
}
$result = @{ exists=($null -ne $task); owned=$owned; name=$name; sid=$sid }
if ($task) {
    $result.enabled = $task.Enabled
    $result.state = [int]$task.State
    $result.lastResult = $task.LastTaskResult
}
$result | ConvertTo-Json -Compress
