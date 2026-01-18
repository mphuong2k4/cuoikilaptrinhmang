Đồ án môn **Lập trình mạng**: giám sát máy tính trong LAN theo mô hình **Server/Agent** (UDP discovery + TCP + WebSocket dashboard).

## Cấu trúc
```
pc-network-monitor/
  src/server/server.py   # TCP server + CLI
  src/server/webapp.py   # Web dashboard
  src/agent/agent.py     # Agent
  src/shared/protocol.py
  docs/                  # báo cáo + sơ đồ
  requirements.txt
```

## Cài đặt
Yêu cầu: Python 3.10+
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Chạy demo
**Server + Dashboard**
```powershell
python -m src.server.webapp
```
Mở:
- `http://localhost:8000`
- hoặc `http://<IP_SERVER>:8000`

**Agent**
```powershell
python -m src.agent.agent --discover
# hoặc:
python -m src.agent.agent --server-host <IP_SERVER>
```

## Cổng mặc định
- UDP discovery: `9999`
- TCP server: `9009`
- Web: `8000`

## Firewall (Windows, nếu mở từ máy khác)
PowerShell (Admin):
```powershell
netsh advfirewall firewall add rule name="PCMonitor Web 8000" dir=in action=allow protocol=TCP localport=8000
netsh advfirewall firewall add rule name="PCMonitor TCP 9009" dir=in action=allow protocol=TCP localport=9009
netsh advfirewall firewall add rule name="PCMonitor UDP 9999" dir=in action=allow protocol=UDP localport=9999
```
