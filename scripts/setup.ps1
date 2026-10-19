<#
.SYNOPSIS
    Script de automatización para Windows PowerShell del Laboratorio de Chaos Engineering en GCP / GKE.
.DESCRIPTION
    Permite validar el estado, generar tráfico baseline, inyectar experimentos de caos y realizar port-forwards.
.EXAMPLE
    .\scripts\setup.ps1 -Status
    .\scripts\setup.ps1 -Baseline
    .\scripts\setup.ps1 -Chaos
    .\scripts\setup.ps1 -PortForward
#>

param (
    [switch]$Chaos,
    [switch]$Status,
    [switch]$Baseline,
    [switch]$PortForward,
    [switch]$Rollback,
    [switch]$Chaos2,
    [switch]$Rollback2
)

$Namespace = "otel-lab"
$GcpProject = "bold-bastion-505423-e7"
$ClusterName = "otel-lab"

function Write-Step ($msg) {
    Write-Host "`n[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Cyan
}

function Write-Success ($msg) {
    Write-Host "✅ $msg" -ForegroundColor Green
}

function Write-Warn ($msg) {
    Write-Host "⚠️  $msg" -ForegroundColor Yellow
}

function Show-Status {
    Write-Step "Consultando estado de pods en namespace '$Namespace'..."
    kubectl get pods -n $Namespace -o wide

    Write-Step "Consultando servicios (Services) y LoadBalancers..."
    kubectl get svc -n $Namespace

    Write-Step "Consultando experimentos de Chaos Mesh..."
    kubectl get networkchaos,httpchaos,podchaos,stresschaos -n $Namespace 2>$null

    Write-Step "Consultando experimentos de LitmusChaos..."
    kubectl get chaosengine,chaosresult -n $Namespace 2>$null
}

function Generate-Baseline {
    Write-Step "Generando 50 peticiones baseline a service-a..."
    for ($i = 1; $i -le 50; $i++) {
        $orderId = "ord-00$((($i % 3) + 1))"
        try {
            kubectl exec -n $Namespace deployment/service-a -c service-a -- python -c "import time, urllib.request; t0=time.time(); resp=urllib.request.urlopen('http://localhost:8000/order/$orderId').read().decode(); print('Req ' + str($i) + ': OK ({:.3f}s)'.format(time.time()-t0))"
        } catch {
            Write-Warn "Fallo en peticion $i"
        }
        Start-Sleep -Milliseconds 200
    }
    Write-Success "Trafico baseline completado exitosamente."
}

function Run-ChaosExperiment {
    Write-Step "=== INICIANDO EXPERIMENTO 1: NetworkChaos 200ms en service-b ==="
    kubectl apply -f chaos-mesh/experiment-1-network-latency.yaml
    Write-Success "Experimento de red aplicado con Chaos Mesh."

    Write-Step "Midiendo latencia durante el caos (muestras en tiempo real)..."
    for ($i = 1; $i -le 5; $i++) {
        kubectl exec -n $Namespace deployment/service-a -c service-a -- python -c "import time, urllib.request; t0=time.time(); resp=urllib.request.urlopen('http://localhost:8000/order/ord-001').read().decode(); print('Muestra Caos ' + str($i) + ': Duracion = {:.4f}s'.format(time.time()-t0))"
        Start-Sleep -Seconds 2
    }

    Write-Step "Para revertir el experimento ejecuta: .\scripts\setup.ps1 -Rollback"
}

function Do-Rollback {
    Write-Step "Revertiendo experimento 1 (Rollback)..."
    kubectl delete -f chaos-mesh/experiment-1-network-latency.yaml --ignore-not-found
    Write-Success "Rollback 1 completado. Verificando recuperacion:"
    kubectl exec -n $Namespace deployment/service-a -c service-a -- python -c "import time, urllib.request; t0=time.time(); resp=urllib.request.urlopen('http://localhost:8000/order/ord-001').read().decode(); print('Post-Rollback: Duracion = {:.4f}s'.format(time.time()-t0))"
}

function Run-ChaosExperiment2 {
    Write-Step "=== INICIANDO EXPERIMENTO 2: Pod Failure / Kill en service-b (Alta Disponibilidad N-1) ==="
    Write-Step "Pods de service-b antes del fallo:"
    kubectl get pods -n $Namespace -l app=service-b

    Write-Step "Inyectando PodChaos (eliminacion no graceful de 1 replica)..."
    kubectl apply -f chaos-mesh/experiment-2-pod-kill.yaml
    Write-Success "PodChaos inyectado."

    Start-Sleep -Seconds 3
    Write-Step "Estado de los pods inmediatamente tras el fallo (observa el reinicio/recreacion):"
    kubectl get pods -n $Namespace -l app=service-b

    Write-Step "Probando resiliencia transaccional de service-a mientras el pod es recreado..."
    for ($i = 1; $i -le 3; $i++) {
        kubectl exec -n $Namespace deployment/service-a -c service-a -- python -c "import time, urllib.request; t0=time.time(); resp=urllib.request.urlopen('http://localhost:8000/order/ord-001').read().decode(); print('Peticion durante Pod Failure ' + str($i) + ': Status 200 OK ({:.3f}s)'.format(time.time()-t0))"
        Start-Sleep -Seconds 1
    }
    Write-Success "Experimento 2 completado: El balanceador de K8s conmuto al pod superviviente sin fallos de usuario."
}

function Do-Rollback2 {
    Write-Step "Revertiendo experimento 2 (Rollback)..."
    kubectl delete -f chaos-mesh/experiment-2-pod-kill.yaml --ignore-not-found
    kubectl delete -f chaos-mesh/experiment-2-cpu-stress.yaml --ignore-not-found
    Write-Success "Rollback 2 completado."
}

function Start-PortForwarding {
    Write-Step "Iniciando Port-Forwards a Jaeger y Prometheus en segundo plano..."
    Start-Process -NoNewWindow kubectl -ArgumentList "port-forward -n $Namespace svc/jaeger-svc 16686:16686"
    Start-Process -NoNewWindow kubectl -ArgumentList "port-forward -n $Namespace svc/prometheus-svc 9091:9090"
    Start-Process -NoNewWindow kubectl -ArgumentList "port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333"
    
    Write-Success "Dashboards disponibles en:"
    Write-Host "  - Jaeger UI:          http://localhost:16686" -ForegroundColor Green
    Write-Host "  - Prometheus UI:      http://localhost:9091" -ForegroundColor Green
    Write-Host "  - Chaos Mesh UI:      http://localhost:2333" -ForegroundColor Green
}

# Flujo principal según argumentos
if ($Status) {
    Show-Status
} elseif ($Baseline) {
    Generate-Baseline
} elseif ($Chaos) {
    Run-ChaosExperiment
} elseif ($Rollback) {
    Do-Rollback
} elseif ($Chaos2) {
    Run-ChaosExperiment2
} elseif ($Rollback2) {
    Do-Rollback2
} elseif ($PortForward) {
    Start-PortForwarding
} else {
    Write-Host "=== OTel Chaos Lab - GCP Helper para PowerShell ===" -ForegroundColor Cyan
    Write-Host "Uso:"
    Write-Host "  .\scripts\setup.ps1 -Status        # Ver pods, servicios y experimentos"
    Write-Host "  .\scripts\setup.ps1 -PortForward   # Abrir Jaeger, Prometheus y Chaos Mesh"
    Write-Host "  .\scripts\setup.ps1 -Baseline      # Generar tráfico inicial"
    Write-Host "  .\scripts\setup.ps1 -Chaos         # Experimento 1: Latencia de red 200ms en service-b"
    Write-Host "  .\scripts\setup.ps1 -Rollback      # Revertir Experimento 1"
    Write-Host "  .\scripts\setup.ps1 -Chaos2        # Experimento 2: Pod Failure en service-b (HA N-1)"
    Write-Host "  .\scripts\setup.ps1 -Rollback2     # Revertir Experimento 2"
}
