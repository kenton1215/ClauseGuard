@echo off
cd /d "%~dp0"
echo ============================================
echo   企业合同审核智能体 正在启动...
echo   浏览器将自动打开 http://localhost:8501
echo   关闭本窗口即停止服务
echo ============================================
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境，请先安装依赖：
    echo python -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8501"
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501
pause
