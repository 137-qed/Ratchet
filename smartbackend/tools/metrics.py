"""
Ratchet — System Metrics Collection Tool
Collects CPU, memory, disk, temperature, battery, network, and process metrics
using psutil.
"""

import psutil
import time
import platform
import subprocess


def get_cpu_metrics():
    """Get CPU utilization and per-core breakdown."""
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_per_core = psutil.cpu_percent(interval=0, percpu=True)
    cpu_count = psutil.cpu_count(logical=True)
    cpu_freq = psutil.cpu_freq()

    return {
        'percent': cpu_percent,
        'per_core': cpu_per_core,
        'core_count': cpu_count,
        'frequency_mhz': cpu_freq.current if cpu_freq else None,
        'frequency_max_mhz': cpu_freq.max if cpu_freq else None,
    }


def get_memory_metrics():
    """Get memory usage statistics."""
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        'percent': mem.percent,
        'total_gb': round(mem.total / (1024 ** 3), 2),
        'used_gb': round(mem.used / (1024 ** 3), 2),
        'available_gb': round(mem.available / (1024 ** 3), 2),
        'swap_percent': swap.percent,
        'swap_total_gb': round(swap.total / (1024 ** 3), 2),
        'swap_used_gb': round(swap.used / (1024 ** 3), 2),
    }


def get_disk_metrics():
    """Get disk usage for all mounted partitions."""
    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                'device': part.device,
                'mountpoint': part.mountpoint,
                'fstype': part.fstype,
                'percent': usage.percent,
                'total_gb': round(usage.total / (1024 ** 3), 2),
                'used_gb': round(usage.used / (1024 ** 3), 2),
                'free_gb': round(usage.free / (1024 ** 3), 2),
            })
        except (PermissionError, OSError):
            continue

    # IO counters
    io = psutil.disk_io_counters()
    io_stats = None
    if io:
        io_stats = {
            'read_mb': round(io.read_bytes / (1024 ** 2), 2),
            'write_mb': round(io.write_bytes / (1024 ** 2), 2),
            'read_count': io.read_count,
            'write_count': io.write_count,
        }

    # Overall disk percent (root partition)
    root_percent = 0
    for p in partitions:
        if p['mountpoint'] == '/':
            root_percent = p['percent']
            break
    if not root_percent and partitions:
        root_percent = partitions[0]['percent']

    return {
        'percent': root_percent,
        'partitions': partitions,
        'io': io_stats,
    }


def get_temperature():
    """Get CPU/system temperature. Works on Linux with sensors."""
    temps = {}

    # Try psutil sensors_temperatures (Linux only)
    if hasattr(psutil, 'sensors_temperatures'):
        try:
            sensor_temps = psutil.sensors_temperatures()
            if sensor_temps:
                for name, entries in sensor_temps.items():
                    for entry in entries:
                        label = entry.label or name
                        temps[label] = {
                            'current': entry.current,
                            'high': entry.high,
                            'critical': entry.critical,
                        }
        except Exception:
            pass

    # Try Raspberry Pi specific
    try:
        result = subprocess.run(
            ['vcgencmd', 'measure_temp'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Output: temp=45.0'C
            temp_str = result.stdout.strip().replace("temp=", "").replace("'C", "")
            temps['raspberry_pi'] = {
                'current': float(temp_str),
                'high': 80.0,
                'critical': 85.0,
            }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    # Get main temperature value
    main_temp = 0.0
    if temps:
        # Prefer coretemp or cpu_thermal
        for key in ['coretemp', 'cpu_thermal', 'raspberry_pi']:
            if key in temps:
                main_temp = temps[key]['current']
                break
        if main_temp == 0.0:
            first_key = list(temps.keys())[0]
            main_temp = temps[first_key]['current']

    return {
        'celsius': main_temp,
        'sensors': temps,
    }


def get_battery():
    """Get battery status if available."""
    battery = psutil.sensors_battery()
    if battery:
        return {
            'percent': battery.percent,
            'power_plugged': battery.power_plugged,
            'seconds_left': battery.secsleft if battery.secsleft != psutil.POWER_TIME_UNLIMITED else None,
        }
    return None


def get_network_metrics():
    """Get network I/O statistics."""
    net_io = psutil.net_io_counters()
    connections = psutil.net_connections(kind='inet')

    # Get network interfaces
    interfaces = {}
    net_if = psutil.net_if_addrs()
    net_stats = psutil.net_if_stats()
    for iface, addrs in net_if.items():
        if_stat = net_stats.get(iface)
        interfaces[iface] = {
            'is_up': if_stat.isup if if_stat else False,
            'speed_mbps': if_stat.speed if if_stat else 0,
            'addresses': [
                {'address': a.address, 'family': str(a.family)}
                for a in addrs
            ]
        }

    return {
        'bytes_sent': net_io.bytes_sent,
        'bytes_recv': net_io.bytes_recv,
        'mbps_sent': round(net_io.bytes_sent / (1024 ** 2), 2),
        'mbps_recv': round(net_io.bytes_recv / (1024 ** 2), 2),
        'packets_sent': net_io.packets_sent,
        'packets_recv': net_io.packets_recv,
        'errors_in': net_io.errin,
        'errors_out': net_io.errout,
        'drops_in': net_io.dropin,
        'drops_out': net_io.dropout,
        'active_connections': len(connections),
        'interfaces': interfaces,
    }


def get_top_processes(n=10):
    """Get top N processes by CPU and memory usage."""
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
        try:
            info = p.info
            procs.append({
                'pid': info['pid'],
                'name': info['name'],
                'cpu_percent': info['cpu_percent'] or 0,
                'memory_percent': round(info['memory_percent'] or 0, 2),
                'status': info['status'],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Sort by CPU, then memory
    procs.sort(key=lambda x: (x['cpu_percent'], x['memory_percent']), reverse=True)
    return procs[:n]


def get_system_info():
    """Get system identification info."""
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)

    return {
        'hostname': platform.node(),
        'os': f"{platform.system()} {platform.release()}",
        'architecture': platform.machine(),
        'python_version': platform.python_version(),
        'boot_time': boot_time,
        'uptime': f"{days}d {hours}h",
        'uptime_seconds': uptime_seconds,
    }


def get_system_metrics():
    """
    Main entry point — collects all system metrics in one call.
    Returns a comprehensive dict of metrics.
    """
    cpu = get_cpu_metrics()
    memory = get_memory_metrics()
    disk = get_disk_metrics()
    temperature = get_temperature()
    battery = get_battery()
    network = get_network_metrics()
    processes = get_top_processes(10)
    system = get_system_info()

    return {
        'timestamp': time.time(),
        'system': system,
        'cpu': cpu,
        'memory': memory,
        'disk': disk,
        'temperature': temperature,
        'battery': battery,
        'network': network,
        'processes': processes,
        'process_count': len(list(psutil.process_iter())),
    }


if __name__ == '__main__':
    import json
    metrics = get_system_metrics()
    print(json.dumps(metrics, indent=2, default=str))
