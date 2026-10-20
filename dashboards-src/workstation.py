#!/usr/bin/env python3
"""Generate the workstation dashboards (Grafana 13 schema v2, tabs, 360 Observability rules).

`python3 dashboards-src/workstation.py` writes umbrella-charts/observability/workstation-dashboards/dashboards/;
`--check` fails when a dashboard breaks a rule (grafana_v2.Dash.check) or the committed JSON is out of date.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grafana_v2 import (  # noqa: E402
    COUNT_TODO,
    INFO,
    RATIO_USED,
    Dash,
    col,
    range_target,
    series,
    stat,
    steps,
    table,
    text,
    tiles,
    timeline,
    v_datasource,
)

OUT = Path(__file__).resolve().parents[1] / "umbrella-charts/observability/workstation-dashboards/dashboards"
TAGS = ["workstation"]
HOME = ("Workstation", "/d/ws-overview")
UPSTREAM = "/dashboards?tag=workstation-upstream"

# Shared expressions -----------------------------------------------------------------------------------------------
CPU = '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'
MEM = "1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)"
FS = 'fstype!~"tmpfs|overlay|squashfs|ramfs|nsfs|devtmpfs|fuse.*"'
DISK = f"max(1 - node_filesystem_avail_bytes{{{FS}}} / node_filesystem_size_bytes{{{FS}}})"
GPU_UTIL = "avg(DCGM_FI_DEV_GPU_UTIL)"
VRAM = "sum(DCGM_FI_DEV_FB_USED) / (sum(DCGM_FI_DEV_FB_USED) + sum(DCGM_FI_DEV_FB_FREE))"
GPU_TEMP = "max(DCGM_FI_DEV_GPU_TEMP)"
# DCGM sometimes reports an impossible power sample (16 kW seen on a laptop GPU): drop readings above 1 kW.
GPU_POWER = "sum(DCGM_FI_DEV_POWER_USAGE < 1000)"
MODEL = 'model_name!=""'
LLM_RPS = "sum(rate(litellm_proxy_total_requests_metric_total[5m]))"
LLM_FAIL = f"sum(rate(litellm_proxy_failed_requests_metric_total[5m])) / ({LLM_RPS})"
LLM_LAT = ("sum(rate(litellm_request_total_latency_metric_sum[5m])) / "
           "sum(rate(litellm_request_total_latency_metric_count[5m]))")
NOT_RUNNING = 'kube_pod_status_phase{phase=~"Pending|Failed|Unknown"} == 1'
# The kubelet reports each claim twice (with and without its namespace): keep the namespaced series.
PVC = ('kubelet_volume_stats_used_bytes{exported_namespace!=""} / '
       'kubelet_volume_stats_capacity_bytes{exported_namespace!=""}')
DEPLOY_MAP = [{"type": "value", "options": {"0": {"text": "healthy", "color": "green", "index": 0},
                                            "1": {"text": "partial outage", "color": "orange", "index": 1},
                                            "2": {"text": "outage", "color": "red", "index": 2}}}]
STATE3 = steps(("grey", None), ("green", 0), ("orange", 1), ("red", 2))


def lbl(expr, value):
    """Give a label-less expression a distinct `k` label, so `or` keeps every branch."""
    return f'label_replace({expr}, "k", "{value}", "", "")'


def worst(*exprs):
    """Worst (max) of several 0 / 1 / 2 states; vector(0) first so an absent source does not hide the others."""
    return "max(" + " or ".join(lbl(e, i) for i, e in enumerate(("vector(0)", *exprs))) + ")"


def level(expr, warn, fail):
    """0 below warn, 1 from warn, 2 from fail."""
    return f"(({expr}) >= bool {warn}) + (({expr}) >= bool {fail})"


NODE_HEALTH = worst(level(MEM, 0.9, 0.97), level(DISK, 0.85, 0.95), f"(({CPU}) >= bool 0.95)")
GPU_HEALTH = worst("2 * (absent(DCGM_FI_DEV_GPU_UTIL) or vector(0))", level(GPU_TEMP, 85, 92),
                   "2 * (max(DCGM_FI_DEV_ROW_REMAP_FAILURE) > bool 0)")
VLLM_DOWN = "2 * (absent(vllm:healthy_pods_total) or vector(0))"
MODELS_HEALTH = worst("2 * (max(vllm:healthy_pods_total) == bool 0)", VLLM_DOWN,
                      level(f"max(vllm:kv_cache_usage_perc{{{MODEL}}})", 0.9, 1.1),
                      f"(sum(vllm:num_requests_waiting{{{MODEL}}}) > bool 0)")
GATEWAY_HEALTH = worst("max(litellm_deployment_state)", '2 * (max(up{job="litellm"}) == bool 0)',
                       level(LLM_FAIL, 0.05, 0.25))
PLATFORM_HEALTH = worst(f"(count({NOT_RUNNING}) > bool 0)", "(count(up == 0) > bool 0)",
                        level(f"max({PVC})", 0.85, 0.95))
AGENTS_DOWN = "(sum(harness_team_agents) or vector(0)) - (count(harness_agent_up == 1) or vector(0))"
AGENTS_HEALTH = worst(f"2 * ({AGENTS_DOWN} > bool 0)",
                      '(sum(harness_agent_activity_state{state=~"retrying|stopping"}) > bool 0)',
                      "2 * (max(harness_team_paused) > bool 0)")


def multistat(title, desc, items, link=None):
    """One tile per (expr, name, unit, thresholds[, text shown without value])."""
    p = stat(title, desc, items[0][0], text_mode="value_and_name", link=link)
    p["targets"] = [range_target(it[0], it[1], chr(65 + i)) | {"instant": True, "range": False}
                    for i, it in enumerate(items)]
    p["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": it[1]},
         "properties": [{"id": "unit", "value": it[2]}, {"id": "thresholds", "value": it[3] or INFO}]
         + ([{"id": "noValue", "value": it[4]}] if len(it) > 4 else [])}
        for it in items]
    p["options"]["text"] = {"titleSize": 12, "valueSize": 14}
    p["options"].update(minVizWidth=140, minVizHeight=40)
    return p


def health_series(title, exprs):
    """Health of the tab objects over time (0 OK, 1 DEGRADED, 2 FAILED)."""
    return series(title, "0 OK, 1 DEGRADED, 2 FAILED.", [range_target(e, n, chr(65 + i))
                                                          for i, (e, n) in enumerate(exprs)], health=True)


def dash(uid, title, desc, kind, subject):
    links = [{"title": "Workstation", "type": "link", "icon": "dashboard", "url": "/d/ws-overview", "keepTime": True,
              "includeVars": False, "targetBlank": False},
             {"title": "Workstation dashboards", "type": "dashboards", "tags": ["workstation"], "asDropdown": True,
              "keepTime": True, "includeVars": False},
             {"title": "Upstream dashboards", "type": "link", "icon": "external link", "url": "/dashboards",
              "keepTime": False, "includeVars": False, "targetBlank": False}]
    return Dash(uid, title, desc, [v_datasource()], tags=TAGS, crumbs=[HOME] if uid != "ws-overview" else [],
                kind=kind, subject=subject, links=links)


# 00 - Overview ------------------------------------------------------------------------------------------------------
def overview():
    d = dash("ws-overview", "00 - Overview", "The workstation at a glance: node, GPU, local models, AI gateway, "
             "Kubernetes platform and agent teams. Each tab opens the detailed dashboard.", "Overview",
             ("Workstation", "k3s"))
    d.health(worst(NODE_HEALTH, GPU_HEALTH, MODELS_HEALTH, GATEWAY_HEALTH, PLATFORM_HEALTH, AGENTS_HEALTH),
             "Worst health of the node, the GPU, the local models, the AI gateway, the platform and the agents.")

    d.section("Node", multistat("Node", "CPU, memory and fullest filesystem of the k3s node.", [
        (CPU, "CPU", "percentunit", RATIO_USED), (MEM, "Memory", "percentunit", steps(("green", None),
                                                                                       ("orange", 0.9), ("red", 0.97))),
        (DISK, "Disk", "percentunit", steps(("green", None), ("orange", 0.85), ("red", 0.95))),
        ("max(node_power_supply_capacity)", "Battery", "percent",
         steps(("red", None), ("orange", 20), ("green", 40)))]),
        NODE_HEALTH)
    d.stats(4, stat("CPU", "Busy share of all cores (5 min).", CPU, RATIO_USED, "percentunit"),
            stat("Memory", "Used share of the memory (available excluded).", MEM,
                 steps(("green", None), ("orange", 0.9), ("red", 0.97)), "percentunit"),
            stat("Disk", "Fullest real filesystem.", DISK, steps(("green", None), ("orange", 0.85), ("red", 0.95)),
                 "percentunit"),
            stat("Load / core", "1 min load average per core.",
                 'max(node_load1) / count(node_cpu_seconds_total{mode="idle"})', RATIO_USED, "percentunit"))
    d.line(8, (series("CPU and memory", "", [range_target(CPU, "CPU", "A"), range_target(MEM, "memory", "B"),
                                              range_target(DISK, "fullest disk", "C")], "percentunit"), 12),
           (series("Network", "Bytes per second on the physical interfaces.", [
               range_target('sum(rate(node_network_receive_bytes_total{device!~"lo|veth.*|cni.*|flannel.*"}[5m]))',
                            "received", "A"),
               range_target('sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|cni.*|flannel.*"}[5m]))',
                            "sent", "B")], "Bps"), 12))
    d.line(5, (table("Filesystems", "Real filesystems of the node and how full they are.",
                     [f"max by (mountpoint, fstype) (node_filesystem_size_bytes{{{FS}}})",
                      f"max by (mountpoint, fstype) (1 - node_filesystem_avail_bytes{{{FS}}} / "
                      f"node_filesystem_size_bytes{{{FS}}})"],
                     {"mountpoint": "Mount", "fstype": "Type", "Value #A": "Size", "Value #B": "Used"},
                     [col("Size", unit="bytes"), col("Used", unit="percentunit", thresholds=steps(
                         ("green", None), ("orange", 0.85), ("red", 0.95)))]), 24))

    d.section("GPU", multistat("GPU", "GPU load, memory, temperature and power. Opens 01 - GPU & models.", [
        (GPU_UTIL, "Load", "percent", None), (VRAM, "VRAM", "percentunit", steps(("blue", None), ("orange", 0.97))),
        (GPU_TEMP, "Temp.", "celsius", steps(("green", None), ("orange", 85), ("red", 92))),
        (GPU_POWER, "Power", "watt", None)]), GPU_HEALTH)
    d.line(8, (series("GPU", "Load and memory of the GPU (vLLM keeps most of the memory for its KV cache).", [
        range_target(GPU_UTIL, "load %", "A"), range_target(f"({VRAM}) * 100", "VRAM %", "B")], "percent"), 12),
        (series("Temperature", "GPU temperature (orange from 85 °C).", [range_target(GPU_TEMP, "GPU", "A")],
                "celsius"), 12))
    d.line(2, (text("Details: [01 - GPU & models](/d/ws-gpu-models?${__url_time_range}) (GPU, models, latency)"),
               24))

    d.section("Models", multistat("Models", "Local models served by vLLM: requests running and queued, KV cache "
                                  "use, generation speed.", [
        (f"count(vllm:num_requests_running{{{MODEL}}})", "Models", "none", None),
        (f"sum(vllm:num_requests_running{{{MODEL}}})", "Running", "none", None),
        (f"sum(vllm:num_requests_waiting{{{MODEL}}})", "Queued", "none", COUNT_TODO),
        (f"max(vllm:kv_cache_usage_perc{{{MODEL}}})", "KV cache", "percentunit", steps(("green", None),
                                                                                        ("orange", 0.9))),
        (f"sum(rate(vllm:generation_tokens_total{{{MODEL}}}[5m]))", "Tokens / s", "none", None)]), MODELS_HEALTH)
    d.line(8, (series("Requests", "Requests running and queued per model.", [
        range_target(f"sum by (model_name) (vllm:num_requests_running{{{MODEL}}})", "{{model_name}} running", "A"),
        range_target(f"sum by (model_name) (vllm:num_requests_waiting{{{MODEL}}})", "{{model_name}} queued", "B")],
        no_value="No request"), 12),
        (series("Generation speed", "Tokens generated per second per model.", [range_target(
            f"sum by (model_name) (rate(vllm:generation_tokens_total{{{MODEL}}}[$__rate_interval]))",
            "{{model_name}}")], no_value="Nothing generated"), 12))
    d.line(4, (table("Models", "Every model vLLM serves now.", [
        f"sum by (model_name) (vllm:num_requests_running{{{MODEL}}})",
        f"sum by (model_name) (vllm:num_requests_waiting{{{MODEL}}})",
        f"max by (model_name) (vllm:kv_cache_usage_perc{{{MODEL}}})",
        f"sum by (model_name) (rate(vllm:generation_tokens_total{{{MODEL}}}[5m]))"],
        {"model_name": "Model", "Value #A": "Running", "Value #B": "Queued", "Value #C": "KV cache",
         "Value #D": "Tokens / s"},
        [col("KV cache", unit="percentunit"), col("Tokens / s", decimals=1)]), 24))

    d.section("Gateway", multistat("Gateway", "LiteLLM: requests, failures, latency and spend over 24 h. Opens 02 - "
                                   "AI gateway.", [
        (f"({LLM_RPS}) * 60", "Requests / min", "none", None),
        (LLM_FAIL, "Failures", "percentunit", steps(("green", None), ("orange", 0.05), ("red", 0.25)), "no request"),
        (LLM_LAT, "Latency", "s", None, "no request"),
        ("sum(increase(litellm_spend_metric_total[1d]))", "Spend (24 h)", "currencyUSD", None)]), GATEWAY_HEALTH)
    d.line(8, (series("Requests per model", "Requests per minute through the gateway, per requested model.", [
        range_target("sum by (requested_model) (rate(litellm_proxy_total_requests_metric_total[$__rate_interval]))"
                     " * 60", "{{requested_model}}")], no_value="No request"), 12),
        (timeline("Deployments", "State of every model deployment behind the gateway.",
                  "max by (litellm_model_name, api_provider) (litellm_deployment_state)",
                  "{{litellm_model_name}} ({{api_provider}})", STATE3, DEPLOY_MAP,
                  "/d/ws-ai-gateway?dtab=deployments&${__url_time_range}", rows=4), 12))
    d.line(2, (text("Details: [02 - AI gateway](/d/ws-ai-gateway?${__url_time_range}) (traffic, errors, "
                    "latency, spend, deployments)"), 24))

    d.section("Platform", multistat("Platform", "Kubernetes: pods not running, containers restarted in the last "
                                    "hour, scrape targets down, fullest volume. Argo CD is not measured yet.", [
        (f"count({NOT_RUNNING}) or vector(0)", "Pods not running", "none", COUNT_TODO),
        ("sum(increase(kube_pod_container_status_restarts_total[1h]))", "Restarts (1 h)", "none", COUNT_TODO),
        ("count(up == 0) or vector(0)", "Targets down", "none", COUNT_TODO),
        (f"max({PVC})", "Fullest volume", "percentunit", steps(("green", None), ("orange", 0.85), ("red", 0.95)))]),
        PLATFORM_HEALTH)
    d.line(8, (series("Pods not running", "Pods Pending, Failed or Unknown per namespace.", [range_target(
        f"count by (namespace) ({NOT_RUNNING})", "{{namespace}}")], no_value="Every pod runs"), 12),
        (series("Restarts", "Container restarts per namespace.", [range_target(
            "sum by (namespace) (increase(kube_pod_container_status_restarts_total[$__rate_interval])) > 0",
            "{{namespace}}")], calcs=("sum",), no_value="No restart"), 12))
    d.line(5, (table("Volumes", "Persistent volumes and how full they are (local-path volumes all report the "
                     "node disk).", [
        f"max by (exported_namespace, persistentvolumeclaim) ({PVC})",
        "max by (exported_namespace, persistentvolumeclaim) "
        '(kubelet_volume_stats_capacity_bytes{exported_namespace!=""})'],
        {"exported_namespace": "Namespace", "persistentvolumeclaim": "Volume", "Value #A": "Used",
         "Value #B": "Size"},
        [col("Volume", fill=True),   # claim names up to ~720 px (Prometheus TSDB claim): wrap
         col("Used", unit="percentunit", thresholds=steps(("green", None), ("orange", 0.85), ("red", 0.95))),
         col("Size", unit="bytes")], sort=("Used", True)), 24))

    d.section("Agents", multistat("Agents", "Agent teams of the harness: agents working, waiting, idle, down, and "
                                  "the team cost today. Opens the Agent harness dashboard.", [
        ('sum(harness_agent_activity_state{state="working"})', "Working", "none", None),
        ('sum(harness_agent_activity_state{state=~"waiting|idle"})', "Waiting / idle", "none", None),
        ('sum(harness_agent_activity_state{state=~"awaiting_human|retrying|paused_.*"})', "To handle", "none",
         COUNT_TODO),
        ("sum(harness_team_cost_usd_today)", "Cost today", "currencyUSD", None)]), AGENTS_HEALTH)
    d.line(4, (tiles("Agents by activity", "Open the Agent harness dashboard for the details.", [
        ('sum(harness_agent_activity_state{state="working"})', "Working", "green"),
        ('sum(harness_agent_activity_state{state="waiting"})', "Waiting", "purple"),
        ('sum(harness_agent_activity_state{state="idle"})', "Idle", "blue"),
        ('sum(harness_agent_activity_state{state="awaiting_human"})', "Awaiting human", "orange"),
        ('sum(harness_agent_activity_state{state=~"retrying|stopping"})', "Retrying", "orange"),
        ('sum(harness_agent_activity_state{state=~"paused_.*"})', "Paused", "red"),
        (AGENTS_DOWN, "Down", "red")],
        link="/d/agent-harness?dtab=activity&${__url_time_range}"), 24))
    d.line(8, (series("Agents working", "Agents running an engine, and the team cost per interval.", [
        range_target('sum(harness_agent_activity_state{state="working"})', "working", "A")],
        no_value="No agent"), 12),
        (series("Team cost", "USD spent per interval by the agent teams.", [range_target(
            "sum by (team) (increase(harness_agent_cost_usd_total[$__rate_interval]))", "{{team}}")],
            "currencyUSD", calcs=("sum",), no_value="Nothing spent"), 12))
    return d


# 01 - GPU & models -------------------------------------------------------------------------------------------------
def gpu_models():
    d = dash("ws-gpu-models", "01 - GPU & models", "The workstation GPU and the local models served by vLLM.",
             "Drill-down", ("Workstation", "GPU & local models"))
    d.health(worst(GPU_HEALTH, MODELS_HEALTH), "Worst of the GPU health and the local models health.")
    d.section("GPU", multistat("GPU", "Load, memory, temperature and power of the GPU.", [
        (GPU_UTIL, "Load", "percent", None), (VRAM, "VRAM", "percentunit", None),
        (GPU_TEMP, "Temp.", "celsius", steps(("green", None), ("orange", 85), ("red", 92))),
        (GPU_POWER, "Power", "watt", None)]), GPU_HEALTH)
    d.stats(4, stat("Load", "SM busy share.", GPU_UTIL, unit="percent"),
            stat("VRAM", "Used framebuffer memory.", VRAM, unit="percentunit"),
            stat("Temp.", "GPU temperature.", GPU_TEMP, steps(("green", None), ("orange", 85), ("red", 92)), "celsius"),
            stat("Memory errors", "Row remap failures (hardware memory errors): any is a fault.",
                 "max(DCGM_FI_DEV_ROW_REMAP_FAILURE)", steps(("green", None), ("red", 1)), no_value="not measured"))
    d.line(8, (series("Load and memory", "", [range_target(GPU_UTIL, "load %", "A"),
                                              range_target(f"({VRAM}) * 100", "VRAM %", "B"),
                                              range_target("avg(DCGM_FI_DEV_MEM_COPY_UTIL)", "memory copy %", "C")],
                      "percent"), 12),
           (series("Temperature", "GPU and memory temperature.", [
               range_target(GPU_TEMP, "GPU", "A"), range_target("max(DCGM_FI_DEV_MEMORY_TEMP) > 0", "memory", "B")],
               "celsius"), 12))
    d.line(8, (series("Power", "Board power draw (readings above 1 kW dropped: DCGM glitch).", [
        range_target(GPU_POWER, "power")], "watt"), 12),
        (series("Clocks", "SM and memory clocks (MHz).", [
        range_target("max(DCGM_FI_DEV_SM_CLOCK)", "SM", "A"),
        range_target("max(DCGM_FI_DEV_MEM_CLOCK)", "memory", "B")],
        "none"), 12))
    d.line(8, (series("PCIe replays", "PCIe replay counter increase: a growing count means link errors.", [range_target(
        "sum(increase(DCGM_FI_DEV_PCIE_REPLAY_COUNTER[$__rate_interval]))", "replays")], calcs=("sum",),
        no_value="No replay"), 24))

    d.section("Models", multistat("Models", "Models served, requests running and queued, KV cache, speed.", [
        (f"count(vllm:num_requests_running{{{MODEL}}})", "Models", "none", None),
        (f"sum(vllm:num_requests_running{{{MODEL}}})", "Running", "none", None),
        (f"sum(vllm:num_requests_waiting{{{MODEL}}})", "Queued", "none", COUNT_TODO),
        (f"sum(rate(vllm:generation_tokens_total{{{MODEL}}}[5m]))", "Tokens / s", "none", None)]), MODELS_HEALTH)
    d.line(8, (series("Requests", "Running and queued requests per model.", [
        range_target(f"sum by (model_name) (vllm:num_requests_running{{{MODEL}}})", "{{model_name}} running", "A"),
        range_target(f"sum by (model_name) (vllm:num_requests_waiting{{{MODEL}}})", "{{model_name}} queued", "B")],
        no_value="No request"), 12),
        (series("KV cache", "Share of the KV cache in use per model; at 100 % requests are preempted.", [range_target(
            f"max by (model_name) (vllm:kv_cache_usage_perc{{{MODEL}}})", "{{model_name}}")], "percentunit"), 12))
    d.line(8, (series("Tokens per second", "Prompt tokens processed and tokens generated.", [
        range_target(f"sum by (model_name) (rate(vllm:prompt_tokens_total{{{MODEL}}}[$__rate_interval]))",
                     "{{model_name}} prompt", "A"),
        range_target(f"sum by (model_name) (rate(vllm:generation_tokens_total{{{MODEL}}}[$__rate_interval]))",
                     "{{model_name}} generated", "B")], no_value="Idle"), 12),
        (series("Prefix cache hits", "Share of prompt tokens served from the prefix cache.", [range_target(
            f"sum by (model_name) (rate(vllm:prefix_cache_hits_total{{{MODEL}}}[$__rate_interval])) / sum by "
            f"(model_name) (rate(vllm:prefix_cache_queries_total{{{MODEL}}}[$__rate_interval]))", "{{model_name}}")],
            "percentunit", no_value="No query"), 12))
    d.line(4, (table("Models", "Every model vLLM serves now.", [
        f"sum by (model_name) (vllm:num_requests_running{{{MODEL}}})",
        f"sum by (model_name) (vllm:num_requests_waiting{{{MODEL}}})",
        f"max by (model_name) (vllm:kv_cache_usage_perc{{{MODEL}}})",
        f"sum by (model_name) (increase(vllm:request_success_total{{{MODEL}}}[1d]))",
        f"sum by (model_name) (increase(vllm:num_preemptions_total{{{MODEL}}}[1d]))"],
        {"model_name": "Model", "Value #A": "Running", "Value #B": "Queued", "Value #C": "KV cache",
         "Value #D": "Done (24 h)", "Value #E": "Preempted"},
        [col("KV cache", unit="percentunit"), col("Done (24 h)", decimals=0),
         col("Preempted", decimals=0, thresholds=COUNT_TODO)]), 24))

    lat = [("time_to_first_token_seconds", "first token"), ("request_queue_time_seconds", "queue"),
           ("e2e_request_latency_seconds", "end to end")]
    d.section("Latency", multistat("Latency", "Average time to the first token and end-to-end request time "
                                   "(5 min).", [
        (f"sum(rate(vllm:{m}_sum{{{MODEL}}}[5m])) / sum(rate(vllm:{m}_count{{{MODEL}}}[5m]))", n, "s", None,
         "no request") for m, n in lat]))
    d.line(8, (series("Latency", "Average per interval.", [range_target(
        f"sum(rate(vllm:{m}_sum{{{MODEL}}}[$__rate_interval])) / sum(rate(vllm:{m}_count{{{MODEL}}}"
        f"[$__rate_interval]))", n, chr(65 + i)) for i, (m, n) in enumerate(lat)], "s", no_value="No request"), 24))
    d.line(8, (series("Inter-token latency", "Average time between two generated tokens.", [range_target(
        f"sum(rate(vllm:inter_token_latency_seconds_sum{{{MODEL}}}[$__rate_interval])) / "
        f"sum(rate(vllm:inter_token_latency_seconds_count{{{MODEL}}}[$__rate_interval]))", "inter-token")], "s",
        no_value="No request"), 24))
    return d


# 02 - AI gateway ---------------------------------------------------------------------------------------------------
def ai_gateway():
    d = dash("ws-ai-gateway", "02 - AI gateway", "LiteLLM, the gateway in front of the local and SaaS models: "
             "traffic, failures, latency, spend and deployments.", "Drill-down", ("AI gateway", "LiteLLM"))
    d.health(GATEWAY_HEALTH, "FAILED: a deployment is out or LiteLLM is down. DEGRADED: a partial outage, or more "
             "than 5 % of the requests fail.")
    d.section("Traffic", multistat("Traffic", "Requests per minute, keys and models in use (24 h).", [
        (f"({LLM_RPS}) * 60", "Requests / min", "none", None),
        ("count(count by (api_key_alias) (increase(litellm_proxy_total_requests_metric_total[1d]) > 0))", "Keys used",
         "none", None),
        ("count(count by (requested_model) (increase(litellm_proxy_total_requests_metric_total[1d]) > 0))",
         "Models used", "none", None),
        ("sum(litellm_in_flight_requests)", "In flight", "none", None)]))
    d.line(8, (series("Requests per model", "Requests per minute per requested model.", [range_target(
        "sum by (requested_model) (rate(litellm_proxy_total_requests_metric_total[$__rate_interval])) * 60",
        "{{requested_model}}")], no_value="No request"), 12),
        (series("Requests per key", "Requests per minute per virtual key.", [range_target(
            "sum by (api_key_alias) (rate(litellm_proxy_total_requests_metric_total[$__rate_interval])) * 60",
            "{{api_key_alias}}")], no_value="No request"), 12))
    d.line(5, (table("Keys and models (24 h)", "Requests per virtual key and model over the last 24 hours.", [
        "sum by (api_key_alias, requested_model) (increase(litellm_proxy_total_requests_metric_total[1d])) > 0",
        "sum by (api_key_alias, requested_model) (increase(litellm_proxy_failed_requests_metric_total[1d]))"],
        {"api_key_alias": "Key", "requested_model": "Model", "Value #A": "Requests", "Value #B": "Failed"},
        [col("Requests", decimals=0), col("Failed", decimals=0, thresholds=COUNT_TODO)], sort=("Requests", True)),
        24))

    d.section("Errors", multistat("Errors", "Failed requests (24 h) and the failure share (5 min).", [
        ("sum(increase(litellm_proxy_failed_requests_metric_total[1d]))", "Failed (24 h)", "none", COUNT_TODO),
        (LLM_FAIL, "Failure share", "percentunit", steps(("green", None), ("orange", 0.05), ("red", 0.25)),
         "no request")]),
        f"({level(LLM_FAIL, 0.05, 0.25)}) or vector(0)")
    d.line(8, (series("Failures", "Failed requests per minute per exception.", [range_target(
        "sum by (exception_class) (rate(litellm_proxy_failed_requests_metric_total[$__rate_interval])) * 60",
        "{{exception_class}}")], no_value="No failure"), 24))
    d.line(5, (table("Failures (24 h)", "Failed requests per key, model and exception.", [
        "sum by (api_key_alias, requested_model, exception_class) "
        "(increase(litellm_proxy_failed_requests_metric_total[1d])) > 0"],
        {"api_key_alias": "Key", "requested_model": "Model", "exception_class": "Exception", "Value": "Failed"},
        [col("Failed", decimals=0)], sort=("Failed", True), no_value="No failure"), 24))

    lat = [("request_total_latency_metric", "total"), ("llm_api_time_to_first_token_metric", "first token"),
           ("overhead_latency_metric", "gateway overhead")]
    d.section("Latency", multistat("Latency", "Average request time, time to the first token and the gateway's own "
                                   "overhead (5 min).", [
        (f"sum(rate(litellm_{m}_sum[5m])) / sum(rate(litellm_{m}_count[5m]))", n, "s", None, "no request")
        for m, n in lat]))
    d.line(8, (series("Latency", "Average per interval.", [range_target(
        f"sum(rate(litellm_{m}_sum[$__rate_interval])) / sum(rate(litellm_{m}_count[$__rate_interval]))", n,
        chr(65 + i)) for i, (m, n) in enumerate(lat)], "s", no_value="No request"), 24))
    d.line(8, (series("Latency per model", "Average total request time per model.", [range_target(
        "sum by (model) (rate(litellm_request_total_latency_metric_sum[$__rate_interval])) / sum by (model) "
        "(rate(litellm_request_total_latency_metric_count[$__rate_interval]))", "{{model}}")], "s",
        no_value="No request"), 24))

    d.section("Spend", multistat("Spend", "What the gateway billed (24 h): local models cost 0.", [
        ("sum(increase(litellm_spend_metric_total[1d]))", "Spend (24 h)", "currencyUSD", None),
        ("sum(increase(litellm_total_tokens_metric_total[1d]))", "Tokens (24 h)", "short", None)]))
    d.line(8, (series("Spend per key", "USD per interval per virtual key.", [range_target(
        "sum by (api_key_alias) (increase(litellm_spend_metric_total[$__rate_interval]))", "{{api_key_alias}}")],
        "currencyUSD", calcs=("sum",), no_value="Nothing spent"), 12),
        (series("Tokens per model", "Tokens per interval per model.", [range_target(
            "sum by (model) (increase(litellm_total_tokens_metric_total[$__rate_interval]))", "{{model}}")],
            calcs=("sum",), no_value="No token"), 12))
    d.line(5, (table("Spend (24 h)", "Spend and tokens per key and model over 24 hours.", [
        "sum by (api_key_alias, model) (increase(litellm_spend_metric_total[1d]))",
        "sum by (api_key_alias, model) (increase(litellm_total_tokens_metric_total[1d]))"],
        {"api_key_alias": "Key", "model": "Model", "Value #A": "Spend", "Value #B": "Tokens"},
        [col("Spend", unit="currencyUSD", decimals=2), col("Tokens", unit="short")], sort=("Spend", True)), 24))

    d.section("Deployments", multistat("Deployments", "Model deployments behind the gateway by state.", [
        ("count(litellm_deployment_state == 0) or vector(0)", "Healthy", "none", None),
        ("count(litellm_deployment_state == 1) or vector(0)", "Partial outage", "none", COUNT_TODO),
        ("count(litellm_deployment_state == 2) or vector(0)", "Outage", "none", steps(("green", None), ("red", 1)))]),
        "max(litellm_deployment_state) or vector(0)")
    d.line(6, (timeline("Deployments over time", "State of every deployment.", "max by (litellm_model_name, "
                        "api_provider, api_base) (litellm_deployment_state)",
                        "{{litellm_model_name}} ({{api_provider}})", STATE3, DEPLOY_MAP, rows=6), 24))
    d.line(4, (table("Deployments", "Every deployment now.", [
        "max by (litellm_model_name, api_provider, api_base) (litellm_deployment_state)"],
        {"litellm_model_name": "Model", "api_provider": "Provider", "api_base": "Endpoint", "Value": "State"},
        [col("Model", width=144),   # qwen3-coder-30b: 128 px measured
         col("State", thresholds=STATE3, mappings=DEPLOY_MAP), col("Endpoint", fill=True)]), 24))
    return d


DASHBOARDS = [overview, gpu_models, ai_gateway]


def main(check=False):
    failed = False
    for build in DASHBOARDS:
        d = build()
        for e in d.check():
            print(f"RULE {d.uid}: {e}")
            failed = True
        rendered = json.dumps(d.render(), indent=1) + "\n"
        path = OUT / f"{d.uid}.json"
        if check:
            if not path.exists() or path.read_text() != rendered:
                print(f"{path.name} is out of date: run dashboards-src/workstation.py")
                failed = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered)
            print(f"wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
