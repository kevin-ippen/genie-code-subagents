"""
MCP Genie Subagent — Setup & Deploy Script

Automates the full deployment lifecycle:
  1. Creates the Databricks App (if it doesn't exist)
  2. Deploys the source code
  3. Prints the MCP connector URL

Prerequisites:
  - Databricks CLI authenticated (`databricks auth login`) or running in a Databricks notebook
  - Source files in the same directory as this script (server.py, app.yaml, requirements.txt)
  - Browser libs generated (run download_browser_libs notebook first)

Usage:
  # From a Databricks notebook cell:
  %run ./setup

  # Or as a standalone script (requires databricks-sdk):
  python setup.py
  python setup.py --app-name my-custom-name
  python setup.py --source-path /Workspace/Users/me@company.com/mcp-genie-subagent
  python setup.py --redeploy  # Force redeploy even if app exists
"""

import argparse
import os
import sys
import time


def get_workspace_client():
    """Get an authenticated WorkspaceClient."""
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient()
    except ImportError:
        print("ERROR: databricks-sdk not installed.")
        print("  pip install databricks-sdk")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not authenticate: {e}")
        print("  Run: databricks auth login")
        sys.exit(1)


def get_current_user(w):
    """Get the current user's email."""
    try:
        me = w.current_user.me()
        return me.user_name
    except Exception as e:
        print(f"WARNING: Could not determine current user: {e}")
        return None


def detect_source_path(w):
    """Auto-detect the source code path based on current user."""
    user = get_current_user(w)
    if user:
        return f"/Workspace/Users/{user}/mcp-genie-subagent"
    return None


def check_libs(source_path):
    """Check if browser libs exist."""
    libs_path = os.path.join(source_path.replace("/Workspace", "/Workspace"), "libs")
    # In notebook context, check directly
    if os.path.exists(libs_path):
        count = len(os.listdir(libs_path))
        if count > 0:
            return True, count
    return False, 0


def setup(app_name="mcp-genie-subagent", source_path=None, redeploy=False):
    """Main setup and deploy flow."""
    from databricks.sdk.service.apps import App, AppDeployment, AppDeploymentMode

    print("=" * 60)
    print("  MCP Genie Subagent — Setup & Deploy")
    print("=" * 60)
    print()

    # 1. Authenticate
    print("[1/5] Authenticating...")
    w = get_workspace_client()
    host = w.config.host
    print(f"  ✓ Connected to: {host}")

    # 2. Detect source path
    print("[2/5] Locating source code...")
    if not source_path:
        source_path = detect_source_path(w)
    if not source_path:
        print("  ERROR: Could not detect source path. Pass --source-path explicitly.")
        sys.exit(1)
    print(f"  ✓ Source: {source_path}")

    # Check for server.py
    try:
        # Try to stat the file via workspace API
        w.workspace.get_status(f"{source_path}/server.py")
        print("  ✓ server.py found")
    except Exception:
        print(f"  WARNING: server.py not found at {source_path}/server.py")
        print("  Make sure you've copied the source files to this location.")
        print("  Continuing anyway (deploy will fail if files are missing)...")

    # 3. Create or get app
    print(f"[3/5] Creating app '{app_name}'...")
    try:
        app = w.apps.get(app_name)
        print(f"  ✓ App already exists: {app.url}")
        if not redeploy:
            print("  Use --redeploy to force a new deployment.")
    except Exception:
        try:
            app = w.apps.create_and_wait(app=App(name=app_name))
            print(f"  ✓ App created: {app.url}")
        except Exception as e:
            if "already exists" in str(e).lower():
                app = w.apps.get(app_name)
                print(f"  ✓ App exists: {app.url}")
            else:
                print(f"  ERROR: {e}")
                sys.exit(1)

    sp_client_id = getattr(app, 'service_principal_client_id', 'unknown')
    print(f"  Service Principal: {sp_client_id}")

    # 4. Deploy
    print("[4/5] Deploying...")
    try:
        deployment = w.apps.deploy_and_wait(
            app_name=app_name,
            app_deployment=AppDeployment(
                source_code_path=source_path,
                mode=AppDeploymentMode.SNAPSHOT
            )
        )
        print(f"  ✓ Deployed successfully!")
        print(f"  Deployment ID: {deployment.deployment_id}")
        status_msg = getattr(deployment.status, 'message', 'OK') if deployment.status else 'OK'
        print(f"  Status: {status_msg}")
    except Exception as e:
        print(f"  ERROR deploying: {e}")
        sys.exit(1)

    # 5. Print connection info
    print("[5/5] Setup complete!")
    print()
    print("=" * 60)
    print("  DEPLOYMENT SUCCESSFUL")
    print("=" * 60)
    print()

    # Extract workspace ID from host for URL construction
    app_url = getattr(app, 'url', f"https://{app_name}-<workspace-id>.aws.databricksapps.com")
    mcp_url = f"{app_url}/mcp" if app_url else f"https://{app_name}-<workspace-id>.aws.databricksapps.com/mcp"

    print(f"  App URL:     {app_url}")
    print(f"  MCP URL:     {mcp_url}")
    print(f"  SP Client:   {sp_client_id}")
    print()
    print("  NEXT STEPS:")
    print("  1. Go to: Databricks Assistant Settings → MCP Connectors")
    print(f"  2. Add connector with URL: {mcp_url}")
    print('  3. Test with: "subagent status"')
    print()
    print("  NOTE: If you previously had a connector, disconnect it first")
    print("  then reconnect (known bug: stale tool schemas after redeploy).")
    print()

    return {
        "app_name": app_name,
        "app_url": app_url,
        "mcp_url": mcp_url,
        "deployment_id": deployment.deployment_id,
        "sp_client_id": sp_client_id,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy MCP Genie Subagent")
    parser.add_argument("--app-name", default="mcp-genie-subagent",
                        help="Databricks App name (default: mcp-genie-subagent)")
    parser.add_argument("--source-path", default=None,
                        help="Workspace path to source code (auto-detected if omitted)")
    parser.add_argument("--redeploy", action="store_true",
                        help="Force redeploy even if app already exists")
    args = parser.parse_args()

    setup(app_name=args.app_name, source_path=args.source_path, redeploy=args.redeploy)
