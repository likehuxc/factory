# D7 Factory Studio

D7 Factory Studio 是面向 D7 生产、调试与售后的 Windows 上位机。项目使用 Python 3.12、PySide6 和 Qt Widgets，统一管理固件升级、电机控制、CAN/CAN FD 诊断、整机操作及设备日志。

## 开发运行

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m d7_factory_studio
pytest
```

硬件操作默认处于锁定状态。没有 USBCANFD-200U 或 Orin 时，应用仍可启动并浏览全部页面，但不会伪造硬件执行成功。

