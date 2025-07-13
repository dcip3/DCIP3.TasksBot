#!/usr/bin/env python3
"""
Quick start script for TasksBot Mini App development.

This script helps you quickly start both the API server and mini app for development.
"""

import subprocess
import sys
import os
import time
import signal
import threading
from pathlib import Path

def get_project_root():
    """Get the project root directory"""
    # Try to find the project root by looking for main.py or main_api.py
    current = Path.cwd()
    while current != current.parent:
        if (current / "main.py").exists() or (current / "main_api.py").exists():
            return current
        current = current.parent
    return Path.cwd()

def run_command(cmd, cwd=None, shell=True):
    """Run a command and return the process"""
    print(f"Running: {cmd}")
    return subprocess.Popen(cmd, cwd=cwd, shell=shell)

def check_dependencies():
    """Check if required dependencies are installed"""
    print("Checking dependencies...")
    
    project_root = get_project_root()
    print(f"Project root: {project_root}")
    
    # Check Python dependencies
    try:
        import fastapi
        import uvicorn
        print("✅ FastAPI dependencies found")
    except ImportError:
        print("❌ FastAPI not found. Install with: pip install fastapi uvicorn")
        return False
    
    # Check Node.js dependencies
    mini_app_path = project_root / "mini-app"
    if not mini_app_path.exists():
        print(f"❌ mini-app directory not found at {mini_app_path}")
        return False
    
    package_json = mini_app_path / "package.json"
    if not package_json.exists():
        print("❌ package.json not found in mini-app directory")
        return False
    
    node_modules = mini_app_path / "node_modules"
    if not node_modules.exists():
        print("⚠️  Node.js dependencies not installed. Run: cd mini-app && npm install")
        return False
    
    print("✅ Node.js dependencies found")
    return True

def start_api_server():
    """Start the FastAPI server"""
    print("\n🚀 Starting API server...")
    project_root = get_project_root()
    return run_command("python main_api.py", cwd=project_root)

def start_mini_app():
    """Start the React mini app development server"""
    print("\n🎨 Starting mini app development server...")
    project_root = get_project_root()
    mini_app_path = project_root / "mini-app"
    return run_command("npm run dev", cwd=mini_app_path)

def main():
    """Main function"""
    print("TasksBot Mini App Development Server")
    print("=" * 40)
    
    # Check dependencies
    if not check_dependencies():
        print("\n❌ Please install missing dependencies and try again.")
        sys.exit(1)
    
    project_root = get_project_root()
    
    # Check if .env file exists
    env_file = project_root / ".env"
    if not env_file.exists():
        print("⚠️  .env file not found. Please create it with your bot token.")
    
    processes = []
    
    try:
        # Start API server
        api_process = start_api_server()
        processes.append(api_process)
        
        # Wait a bit for API to start
        time.sleep(3)
        
        # Start mini app
        app_process = start_mini_app()
        processes.append(app_process)
        
        print("\n✅ Both servers started!")
        print("\n📱 API Server: http://localhost:8000")
        print("🎨 Mini App: http://localhost:3000")
        print("📖 API Docs: http://localhost:8000/docs")
        print("\nPress Ctrl+C to stop all servers")
        
        # Wait for processes
        for process in processes:
            process.wait()
            
    except KeyboardInterrupt:
        print("\n\n🛑 Stopping servers...")
        for process in processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        print("✅ Servers stopped")

if __name__ == "__main__":
    main() 