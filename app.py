import os, json, shutil
import ast
import git
from flask import Flask, render_template, request, jsonify, session
from dotenv import load_dotenv
import requests
import threading, logging
import hashlib
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(level=logging.INFO)

load_dotenv()
app = Flask(__name__)
app.secret_key = "123"  # Replace with a secure random string

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
API_KEY = "sk-7f96e82b77a34e29bd6c1160c2056a38"  # From your input

progress_data = {}

SKIP_PROCESSING_LVL = 2

def extract_function_calls(function_code):
    """
    Extract function calls from a given function code
    Returns a list of function names that are called within the function
    """
    try:
        # Parse the function code
        tree = ast.parse(function_code)
        
        # Store all function calls
        function_calls = []
        
        # Walk through the AST to find all function calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Get the function name being called
                if isinstance(node.func, ast.Name):
                    # Direct function call like function_name()
                    function_calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    # Method call like object.method()
                    function_calls.append(node.func.attr)
        
        # Remove duplicates and return
        return list(set(function_calls))
    except Exception as e:
        app.logger.error(f"Error extracting function calls: {str(e)}")
        return []

def generate_repo_id(repo_url):
    """Create unique ID from URL"""
    # Normalize URL
    parsed = urlparse(repo_url)
    if parsed.netloc == 'github.com':
        path = parsed.path.lower().rstrip('.git')
    else:
        path = parsed.geturl().lower()
    
    # Create SHA256 hash
    return hashlib.sha256(path.encode()).hexdigest()[:12]

def get_repo_path(repo_url):
    """Get local path for repository"""
    repo_id = generate_repo_id(repo_url)
    return os.path.join(os.getcwd(), "repos", repo_id)

def clone_or_update_repo(repo_url):
    repo_path = get_repo_path(repo_url)
    os.makedirs(os.path.dirname(repo_path), exist_ok=True)
    
    try:
        if os.path.exists(os.path.join(repo_path, '.git')):
            # Existing repository - pull latest changes
            repo = git.Repo(repo_path)
            origin = repo.remotes.origin
            origin.pull()
            app.logger.info(f"Updated existing repository at {repo_path}")
        else:
            # Clone new repository
            if os.path.exists(repo_path):
                shutil.rmtree(repo_path)
            git.Repo.clone_from(repo_url, repo_path)
            app.logger.info(f"Cloned new repository to {repo_path}")
            
        return repo_path
    except git.exc.GitCommandError as e:
        app.logger.error(f"Git operation failed: {str(e)}")
        raise RuntimeError(f"Repository management failed: {e.stderr}")

def analyze_readme(repo_dir):
    readme_path = os.path.join(repo_dir, "README.md")
    
    try:
        with open(readme_path, "r") as f:
            readme_content = f.read()
    except FileNotFoundError:
        return "No README found"

    prompt = f"""Analyze this README and identify the main entry file and key functionality. 
    Your answer should only be a json that has an entry "entries", each main entry is an element of "entries".
    "entries" should have entries "func_path" and "desc" where the path is just function name if at top level. File:\n{readme_content}"""
    
    try:
        response = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "messages": [{"role": "user", "content": prompt}],
                "model": "deepseek-coder",
                "max_tokens": 500  # 👈 Set max token count
            },
            timeout=30
        )
        
        # Log the response
        app.logger.info(f"DeepSeek API Response: {response.json()}")
        
        if response.status_code != 200:
            app.logger.error(f"API Error: {response.status_code} - {response.text}")
            return "Analysis failed"

        # Extract the content from the response
        content = response.json()['choices'][0]['message']['content']
        
        # The content is likely wrapped in ```json ... ``` code blocks
        # Let's strip those and parse the JSON content
        if content.startswith('```json'):
            content = content.replace('```json', '', 1)
        if content.endswith('```'):
            content = content[:-3]
        
        # Parse the JSON string into a Python dictionary
        parsed_content = json.loads(content.strip())
        return parsed_content

        
    except Exception as e:
        app.logger.error(f"API Call Failed: {str(e)}")
        return "Analysis failed"
    
def parse_functions(file_path):
    try:
        with open(file_path, "r") as file:
            source_code = file.read()
            
        tree = ast.parse(source_code)
        
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # Get the source segment using the already read source code
                code_segment = ast.get_source_segment(source_code, node)
                functions.append({
                    "original_name": node.name,
                    "code": code_segment
                })
        return functions
        
    except Exception as e:
        app.logger.error(f"Error parsing functions in {file_path}: {str(e)}")
        return []
    
def generate_function_summary(function):
    prompt = f"""Summarize this function in 5-10 words for display, keep original name.
    Then in 5-10 sentences, write a workflow description of the function.
    Original name: {function['original_name']}
    Code:
    {function['code']}
    Respond in JSON format: {{"summary": "...", "description": "..."}}"""
    
    try:
        response = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "messages": [{"role": "user", "content": prompt}],
                "model": "deepseek-coder",
                "max_tokens": 500,
            },
        )
        
        if response.status_code != 200:
            app.logger.error(f"API Error in function summary: {response.status_code} - {response.text}")
            return {"summary": function['original_name'], "description": "No description available"}
            
        content = response.json()['choices'][0]['message']['content']
        app.logger.info(content)
        
        # Clean up the JSON string if needed
        if content.startswith('```json'):
            content = content.replace('```json', '', 1)
        if content.endswith('```'):
            content = content[:-3]
            
        app.logger.info(content.strip())
            
        return json.loads(content.strip())
        
    except Exception as e:
        app.logger.error(f"Error generating function summary: {str(e)}")
        return {"summary": function['original_name'], "description": "No description available"}
    
def build_hierarchy(repo_dir, repo_url=None):
    hierarchy = []
    
    # Track progress for this repository
    repo_id = None
    if repo_url:
        repo_id = generate_repo_id(repo_url)
        progress_data[repo_id] = {
            'status': 'processing',
            'progress': 0,
            'current_function': '',
            'current_index': 0,
            'total_functions': 0
        }
    
    # Get entry file from README analysis
    if SKIP_PROCESSING_LVL < 1:
        readme_analysis = analyze_readme(repo_dir)
    
    # Find the main entry file from the README analysis
    entry_file = "run_nerf.py"  # Default
    if SKIP_PROCESSING_LVL < 1:
        if "entries" in readme_analysis and len(readme_analysis["entries"]) > 0:
            # Extract the first entry's func_path
            main_entry = readme_analysis["entries"][0]["func_path"]
            # If it contains parameters, extract just the filename
            if " " in main_entry:
                entry_file = main_entry.split(" ")[0]
            else:
                entry_file = main_entry
    
    app.logger.info(f"Using entry file: {entry_file}")
    
    # Parse entry file
    entry_path = os.path.join(repo_dir, entry_file)
    
    if SKIP_PROCESSING_LVL > 1:
        hierarchy = [
            {
                "summary": 'Parses configuration arguments for neural rendering',
                "original": 'config_parser',
                "description": 'The config_parser function sets up and returns an argument parser for configuring a neural rendering system. It handles various parameters including experiment setup (name, directories), training options (network architecture, learning rates), rendering settings (sample counts, view directions), and dataset specifications (LLFF/Blender formats). The parser supports both command-line arguments and configuration files, with defaults provided for most parameters. It includes specialized options for view synthesis techniques like slow-motion rendering and bullet time effects. The function ultimately returns the configured parser object which can be used to process input arguments.',
                "children": [],  # Function calls will be populated on demand
                "code": "def config_parser():\n    # Function code here"  # Store code for later extraction
            },
            {
                "summary": 'Train neural radiance field model',
                "original": 'train',
                "description": 'The function loads LLFF dataset, processes poses and images, initializes a neural radiance field model, and trains it with optical flow and depth supervision. It handles various rendering modes (bullet time, slow motion), implements loss functions for scene flow and rendering, and periodically saves checkpoints and validation results. The training loop includes learning rate decay, hard mining for motion regions, and multi-stage optimization with different loss weights.',
                "children": [],  # Function calls will be populated on demand
                "code": "def train():\n    # Function code here"  # Store code for later extraction
            }]
        return hierarchy
    
    # Check if the file exists before attempting to parse it
    if not os.path.isfile(entry_path):
        app.logger.error(f"Entry file does not exist: {entry_path}")
        hierarchy.append({
            "summary": "Error",
            "original": "error",
            "description": f"Entry file {entry_file} not found in repository",
            "children": [],
            "code": ""
        })
        
        # Update progress to complete with error
        if repo_id:
            progress_data[repo_id]['status'] = 'complete'
            progress_data[repo_id]['progress'] = 100
            
        return hierarchy
    
    # parse entry functions
    try:
        functions = parse_functions(entry_path)
        total_functions = len(functions)
        
        # Update total functions count
        if repo_id:
            progress_data[repo_id]['total_functions'] = total_functions
            
        for i, func in enumerate(functions):
            try:
                # Update progress
                if repo_id:
                    progress_data[repo_id]['current_function'] = func['original_name']
                    progress_data[repo_id]['current_index'] = i + 1
                    progress_data[repo_id]['progress'] = int((i / total_functions) * 100)
                
                summary_response = generate_function_summary(func)
                
                # Parse the JSON from the response
                if isinstance(summary_response, str):
                    summary_data = json.loads(summary_response)
                else:
                    summary_data = summary_response
                
                hierarchy.append({
                    "summary": summary_data.get('summary', func['original_name']),
                    "original": func['original_name'],
                    "description": summary_data.get('description', 'No description available'),
                    "children": [],  # Will be populated on demand
                    "code": func['code']  # Store the code for later use
                })
                
            except Exception as e:
                app.logger.error(f"Error processing function {func['original_name']}: {str(e)}")
                # Add a basic entry if summarization fails
                hierarchy.append({
                    "summary": func['original_name'],
                    "original": func['original_name'],
                    "description": "Function summary unavailable",
                    "children": [],
                    "code": func['code']
                })
                
    except Exception as e:
        app.logger.error(f"Error parsing entry file {entry_path}: {str(e)}")
        # Add an error entry to the hierarchy
        hierarchy.append({
            "summary": "Error",
            "original": "error",
            "description": f"Could not analyze entry file: {str(e)}",
            "children": [],
            "code": ""
        })
    
    # Mark progress as complete
    if repo_id:
        progress_data[repo_id]['status'] = 'complete'
        progress_data[repo_id]['progress'] = 100
    
    return hierarchy

@app.route("/")
def entry():
    return render_template("entry.html")

@app.route("/analyze", methods=["POST"])
def analyze_repo():
    try:
        repo_url = request.json.get('repo_url')
        if not repo_url:
            return jsonify({
                "status": "error",
                "message": "Repository URL is required"
            }), 400
            
        repo_path = clone_or_update_repo(repo_url)
        hierarchy = build_hierarchy(repo_path, repo_url)
        
        # Store hierarchy in session
        session['hierarchy'] = hierarchy
        
        return jsonify({
            "status": "success",
            "repo_id": generate_repo_id(repo_url),
            "hierarchy": hierarchy
        })
    except Exception as e:
        app.logger.error(f"Error analyzing repository: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route("/get_function_calls", methods=["POST"])
def get_function_calls():
    try:
        function_code = request.json.get('function_code')
        function_name = request.json.get('function_name', 'Unknown Function')
        
        if not function_code:
            return jsonify({
                "status": "error",
                "message": "Function code is required"
            }), 400
            
        # Extract function calls from the provided code
        function_calls = extract_function_calls(function_code)
        
        # Create child items for each function call
        children = []
        for call in function_calls:
            children.append({
                "summary": call,
                "original": call,
                "description": f"Function call from {function_name}",
                "children": [],
                "is_call": True
            })
        
        return jsonify({
            "status": "success",
            "function_calls": function_calls,
            "children": children
        })
    except Exception as e:
        app.logger.error(f"Error extracting function calls: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500
        
                
@app.route("/progress", methods=["POST"])
def check_progress():
    repo_url = request.json.get('repo_url')
    repo_id = generate_repo_id(repo_url)
    
    # Get progress data for this repository
    repo_progress = progress_data.get(repo_id, {
        'status': 'initializing',
        'progress': 0,
        'current_function': '',
        'current_index': 0,
        'total_functions': 0
    })
    
    return jsonify(repo_progress)

@app.route("/results")
def show_results():
    # Get hierarchy from session or provide empty list if not found
    hierarchy_data = session.get('hierarchy', [])
    return render_template("index.html", hierarchy=hierarchy_data)


@app.route("/repos", methods=["GET"])
def list_repos():
    repo_dir = os.path.join(os.getcwd(), "repos")
    return jsonify({
        "repositories": [
            {"id": d, "path": os.path.join(repo_dir, d)}
            for d in os.listdir(repo_dir)
            if os.path.isdir(os.path.join(repo_dir, d))
        ]
    })
    
if __name__ == "__main__":
    app.run(host='0.0.0.0',debug=True,port=6001)