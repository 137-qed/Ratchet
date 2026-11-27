"""
Ratchet — LLM-Powered Supervisor Agent
Connects directly to an LLM provider (cloud or local llama.cpp) for intelligent
reasoning over device metrics. Falls back to rule-based logic if LLM is unavailable.
"""

import json
import time
import logging
import os

logger = logging.getLogger('ratchet.agent')

# Tool definitions for function calling (OpenAI-compatible format)
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_system_metrics",
            "description": "Collect current system metrics including CPU, memory, disk, temperature, network, and top processes.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_recent_logs",
            "description": "Read recent system logs from journalctl, dmesg, and syslog. Returns errors and log entries from the last few minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to return (default: 50)",
                    },
                    "since_minutes": {
                        "type": "integer",
                        "description": "Read logs from the last N minutes (default: 5)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_service",
            "description": "Restart a systemd service by name. Use this to restart misbehaving services.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "Name of the systemd service to restart",
                    },
                },
                "required": ["service_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_process",
            "description": "Kill a process by PID. Use this for runaway processes consuming too many resources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {
                        "type": "integer",
                        "description": "Process ID to kill",
                    },
                    "signal": {
                        "type": "integer",
                        "description": "Signal to send (default: 9 = SIGKILL)",
                    },
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_cache",
            "description": "Clear system caches (page cache, old /tmp files). Helps free memory and disk space.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_alert",
            "description": "Send an alert/notification through configured channels (Telegram, email, or local report).",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Alert message content",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Alert subject line (for email)",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_report",
            "description": "Write a JSON report to the reports directory documenting an anomaly, diagnosis, or action taken.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "description": "Report data to write",
                    },
                    "report_type": {
                        "type": "string",
                        "description": "Type of report: anomaly, diagnosis, action, summary",
                    },
                },
                "required": ["data", "report_type"],
            },
        },
    },
]

# System prompt that defines the agent's persona and behavior
SYSTEM_PROMPT = """You are Ratchet, an autonomous edge device supervisor agent.

Your job is to continuously monitor edge devices by analyzing system metrics and logs,
diagnose problems, and take corrective actions when safe to do so.

## Your Capabilities
You have access to the following tools:
- **get_system_metrics**: Collect CPU, memory, disk, temperature, network stats
- **read_recent_logs**: Read system logs for errors and warnings
- **restart_service**: Restart a systemd service
- **kill_process**: Kill a runaway process by PID
- **clear_cache**: Clear system caches to free memory/disk
- **send_alert**: Send notifications via Telegram/email
- **write_report**: Document anomalies and actions

## Decision Framework
1. First, analyze the metrics provided to you
2. If metrics are abnormal (CPU > 85%, Memory > 80%, Disk > 85%, Temp > 70°C):
   - Read system logs to understand the cause
   - Diagnose the root cause
   - If safe, take corrective action (kill runaway process, clear cache, restart service)
   - Send an alert and write a report
3. If metrics are normal, report a healthy status
4. Only take safe actions — never reboot the device without explicit confirmation
5. Be concise in your analysis

## Response Format
Always respond with a structured JSON analysis:
{
  "status": "healthy|warning|critical",
  "analysis": "Brief description of system state",
  "anomalies": ["list of detected issues"],
  "actions_taken": ["list of actions performed"],
  "recommendations": ["list of suggestions"]
}
"""


def _get_llm_client(config):
    """Create an OpenAI-compatible client for the active provider."""
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        return None

    base_url = os.environ.get('LLAMA_CPP_BASE_URL', 'http://127.0.0.1:8080/v1')
    api_key = os.environ.get('LLAMA_CPP_API_KEY', 'llama.cpp')

    # Determine the model to use
    models = config.get('llm_models', [])
    active_model = next((m for m in models if m.get('active')), None)
    model_name = active_model['name'] if active_model else 'llama3.2'

    # Map provider to appropriate base URL
    if active_model:
        provider = active_model.get('provider', '').lower()
        if 'openai' in provider:
            base_url = os.environ.get('OPENAI_BASE_URL',
                                       'https://api.openai.com/v1')
            api_key = os.environ.get('OPENAI_API_KEY', 'not-required')
        elif 'anthropic' in provider:
            base_url = os.environ.get('ANTHROPIC_BASE_URL',
                                       'https://api.anthropic.com/v1')
            api_key = os.environ.get('ANTHROPIC_API_KEY', 'not-required')
        elif 'google' in provider:
            base_url = os.environ.get('GOOGLE_BASE_URL',
                                       'https://generativelanguage.googleapis.com/v1beta/openai')
            api_key = os.environ.get('GOOGLE_API_KEY', 'not-required')

    client = OpenAI(base_url=base_url, api_key=api_key)
    return client, model_name


def _execute_tool_call(tool_name, arguments):
    """Execute a tool call and return the result."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    from tools.metrics import get_system_metrics
    from tools.logs import read_recent_logs
    from tools.actions import restart_service, kill_process, clear_cache
    from tools.alerts import send_all_alerts, write_report

    try:
        if tool_name == 'get_system_metrics':
            return get_system_metrics()
        elif tool_name == 'read_recent_logs':
            return read_recent_logs(
                lines=arguments.get('lines', 50),
                since_minutes=arguments.get('since_minutes', 5),
            )
        elif tool_name == 'restart_service':
            return restart_service(arguments['service_name'])
        elif tool_name == 'kill_process':
            return kill_process(
                arguments['pid'],
                signal=arguments.get('signal', 9),
            )
        elif tool_name == 'clear_cache':
            return clear_cache()
        elif tool_name == 'send_alert':
            # Load config for alert channels
            import yaml
            config_path = os.environ.get('RATCHET_CONFIG_PATH', os.path.join(
                os.path.dirname(os.path.dirname(__file__)), 'config.yaml'))
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            return send_all_alerts(
                arguments['message'],
                subject=arguments.get('subject', 'Ratchet Alert'),
                config=cfg.get('alerts', {}),
            )
        elif tool_name == 'write_report':
            return write_report(
                arguments.get('data', {}),
                report_type=arguments.get('report_type', 'agent'),
            )
        else:
            return {'error': f'Unknown tool: {tool_name}'}
    except Exception as e:
        logger.error(f"Tool execution error ({tool_name}): {e}")
        return {'error': str(e)}


def run_agent_cycle(metrics, config):
    """
    Run one LLM-powered agent cycle.

    Sends current metrics to the LLM, lets it reason and optionally call tools,
    then returns structured results.
    """
    result = {
        'events': [],
        'anomalies': [],
        'actions': [],
    }

    client_info = _get_llm_client(config)
    if not client_info:
        return result

    client, model_name = client_info

    # Build the conversation with metrics context
    metrics_summary = json.dumps({
        'cpu_percent': metrics.get('cpu', {}).get('percent', 0),
        'memory_percent': metrics.get('memory', {}).get('percent', 0),
        'disk_percent': metrics.get('disk', {}).get('percent', 0),
        'temperature_celsius': metrics.get('temperature', {}).get('celsius', 0),
        'process_count': metrics.get('process_count', 0),
        'top_processes': metrics.get('processes', [])[:5],
        'network_connections': metrics.get('network', {}).get('active_connections', 0),
        'uptime': metrics.get('system', {}).get('uptime', ''),
    }, indent=2)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Analyze these current metrics and take action if needed:\n\n```json\n{metrics_summary}\n```"},
    ]

    try:
        # Call LLM with tool definitions
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=1024,
        )

        message = response.choices[0].message

        # Process any tool calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)

                logger.info(f"LLM called tool: {fn_name}({fn_args})")
                result['events'].append({
                    'type': 'action_taken',
                    'message': f"🔧 AI called tool: {fn_name}",
                    'severity': 'warning',
                })

                tool_result = _execute_tool_call(fn_name, fn_args)

                result['actions'].append({
                    'tool': fn_name,
                    'args': fn_args,
                    'result': str(tool_result)[:200],
                })

        # Parse the LLM's text response
        content = message.content or ''
        if content:
            try:
                # Try to parse as JSON
                analysis = json.loads(content)
                status = analysis.get('status', 'healthy')

                result['events'].append({
                    'type': 'diagnosis',
                    'message': f"🧠 AI Analysis: {analysis.get('analysis', content[:200])}",
                    'severity': 'critical' if status == 'critical'
                               else 'warning' if status == 'warning'
                               else 'info',
                })

                for anomaly_msg in analysis.get('anomalies', []):
                    result['anomalies'].append({
                        'anomaly_type': status,
                        'metric_name': 'ai_detected',
                        'value': 0,
                        'threshold': 0,
                        'message': anomaly_msg,
                    })

            except json.JSONDecodeError:
                # Plain text response
                result['events'].append({
                    'type': 'diagnosis',
                    'message': f"🧠 AI: {content[:300]}",
                    'severity': 'info',
                })

        logger.info(f"LLM cycle complete — {len(result['actions'])} actions, "
                     f"{len(result['anomalies'])} anomalies")

    except Exception as e:
        logger.error(f"LLM API call failed: {e}")
        result['events'].append({
            'type': 'error',
            'message': f"LLM call failed: {str(e)[:150]}",
            'severity': 'warning',
        })

    return result


def _chat_messages(message, history):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from tools.metrics import get_system_metrics

    try:
        metrics = get_system_metrics()
        context = json.dumps({
            'cpu': metrics.get('cpu', {}).get('percent', 0),
            'memory': metrics.get('memory', {}).get('percent', 0),
            'disk': metrics.get('disk', {}).get('percent', 0),
            'temp': metrics.get('temperature', {}).get('celsius', 0),
            'processes': metrics.get('process_count', 0),
        })
    except Exception:
        context = "{}"

    messages = [
        {
            "role": "system",
            "content": (
                "You are Ratchet, an AI-powered edge device supervisor. "
                "You help users understand their device health, diagnose issues, "
                "and recommend actions.\n\n"
                f"Current device metrics: {context}\n\n"
                "Be helpful, concise, and technical when needed. "
                "Use markdown formatting in your responses. "
                "Answer general and hypothetical questions directly in prose. "
                "Use tools only when needed for a request about the real device. "
                "If the user asks for a text-only answer or says no tools, answer in prose."
            ),
        },
    ]

    # Add conversation history
    for h in history[-8:]:
        messages.append({
            "role": h.get('role', 'user'),
            "content": h.get('content', ''),
        })

    # Add the current message
    messages.append({"role": "user", "content": message})
    return messages


def _stream_chat_completion(client, model_name, messages, tool_calls=None, *, llama_tools=False):
    from openai import APIError

    options = {} if tool_calls is None else {"tools": TOOL_SCHEMAS, "tool_choice": "auto"}
    if llama_tools and tool_calls is not None:
        # Llama 3.2's user-message tool template asks for a function call even on general questions.
        options.update(parallel_tool_calls=False, extra_body={"chat_template_kwargs": {"tools_in_user_message": False}})
    parts = []
    finish_reason = None

    try:
        with client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.7,
            max_tokens=1024,
            stream=True,
            **options,
        ) as completion:
            for chunk in completion:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason

                if tool_calls is not None:
                    for fragment in delta.tool_calls or ():
                        call = tool_calls.setdefault(fragment.index, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if fragment.id:
                            call["id"] = fragment.id
                        if fragment.function:
                            call["function"]["name"] += fragment.function.name or ""
                            call["function"]["arguments"] += fragment.function.arguments or ""

                if delta.content:
                    parts.append(delta.content)
                    yield delta.content
                else:
                    yield ""
    except APIError as error:
        if not llama_tools or tool_calls is None or parts or "expected peg-native format" not in str(error):
            raise
        logger.warning("llama.cpp rejected a tool response; retrying Doc without tools")
        tool_calls.clear()
        return (yield from _stream_chat_completion(client, model_name, messages))

    if finish_reason is None:
        raise RuntimeError("Model stream ended without a finish reason")
    if tool_calls and finish_reason not in ("tool_calls", "stop"):
        raise RuntimeError("Model stream ended with an incomplete tool call")
    return "".join(parts)


def stream_chat_with_agent(message, history, config):
    """Yield reply text and empty deltas for progress that has no visible text."""
    client_info = _get_llm_client(config)
    if not client_info:
        raise RuntimeError("Start the local llama.cpp server or configure an LLM provider")

    client, model_name = client_info
    with client:
        messages = _chat_messages(message, history)
        active_model = next((model for model in config.get('llm_models', []) if model.get('active')), {})
        llama_tools = 'llama.cpp' in active_model.get('provider', 'llama.cpp').lower()
        tool_calls = {}
        opening_reply = yield from _stream_chat_completion(client, model_name, messages, tool_calls, llama_tools=llama_tools)
        if not tool_calls:
            if not opening_reply:
                yield "I couldn't generate a response."
            return

        calls = [tool_calls[index] for index in sorted(tool_calls)]
        pending_tools = []
        for call in calls:
            function = call["function"]
            arguments = json.loads(function["arguments"])
            if not call["id"] or not function["name"] or not isinstance(arguments, dict):
                raise ValueError("Invalid tool call in model response")
            pending_tools.append((call["id"], function["name"], arguments))

        messages.append({"role": "assistant", "content": opening_reply, "tool_calls": calls})
        for call_id, name, arguments in pending_tools:
            result = _execute_tool_call(name, arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, default=str)[:2000],
            })

        if opening_reply:
            yield "\n\n"
        final_reply = yield from _stream_chat_completion(client, model_name, messages)
        if not final_reply:
            yield "Analysis complete."


def chat_with_agent(message, history, config):
    """Collect a complete reply for clients that request JSON."""
    try:
        return "".join(stream_chat_with_agent(message, history, config))
    except Exception as e:
        logger.error(f"Chat LLM error: {e}")
        return (f"⚠️ LLM connection error: {str(e)[:100]}\n\n"
                f"Make sure your LLM provider is configured correctly.")
