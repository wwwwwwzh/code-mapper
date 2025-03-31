import ast as std_ast
import json
import os
from pathlib import Path
from collections import defaultdict

class CallTreeBuilder(std_ast.NodeVisitor):
    def __init__(self, current_module, file_path):
        self.current_module = current_module
        self.file_path = file_path
        self.call_graph = defaultdict(list)
        self.current_context = []  # Stack for tracking context
        self.source_lines = None
        self.ast_map = {}  # Map of function nodes by qualified name
        
        # Read the source file to extract line information
        with open(file_path, 'r', encoding='utf-8') as f:
            self.source_lines = f.readlines()
            
    def get_source_line(self, node):
        """Get the actual source line for a node"""
        if not hasattr(node, 'lineno'):
            return None
        
        if self.source_lines:
            # AST line numbers are 1-based
            return self.source_lines[node.lineno - 1].strip()
        return None
        
    def visit_Module(self, node):
        # Add main block as "__main__"
        main_name = f"{self.current_module}.__main__"
        self.call_graph[main_name] = []
        self.current_context.append(main_name)
        self.generic_visit(node)
        self.current_context.pop()
        
    def visit_FunctionDef(self, node):
        qual_name = f"{self.current_module}.{node.name}"
        self.current_context.append(qual_name)
        self.call_graph[qual_name] = []
        
        # Store AST node reference for function definitions
        self.ast_map[qual_name] = {
            'node': node,
            'ast_id': id(node),
            'line_range': (node.lineno, self._get_last_line(node))
        }
        
        self.generic_visit(node)
        self.current_context.pop()
        
    def _get_last_line(self, node):
        """Get the last line number of a node"""
        # Find the maximum line number in the node
        if hasattr(node, 'end_lineno') and node.end_lineno is not None:
            return node.end_lineno
            
        max_line = getattr(node, 'lineno', 0)
        for field, value in std_ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, std_ast.AST):
                        item_last_line = self._get_last_line(item)
                        max_line = max(max_line, item_last_line)
            elif isinstance(value, std_ast.AST):
                value_last_line = self._get_last_line(value)
                max_line = max(max_line, value_last_line)
        return max_line
        
    def visit_If(self, node):
        # Handle __name__ == "__main__" blocks
        if self._is_main_check(node.test):
            main_name = f"{self.current_module}.__main__"
            current_context_len = len(self.current_context)
            
            # Only append to context if we're not already in __main__ context
            if not self.current_context or self.current_context[-1] != main_name:
                self.current_context.append(main_name)
                
            # Visit the body of the if statement to find calls
            for subnode in node.body:
                self.visit(subnode)
                
            # Pop if we added to the context
            if len(self.current_context) > current_context_len:
                self.current_context.pop()
        else:
            self.generic_visit(node)
            
    def visit_Call(self, node):
        if self.current_context:
            caller = self.current_context[-1]
            callee, is_internal = self._get_call_name(node.func)
            
            if callee and is_internal:
                # Only add internal function calls
                self.call_graph[caller].append({
                    'name': callee,
                    'ast_id': id(node),
                    'lineno': node.lineno,
                    'source': self.get_source_line(node)
                })
                
    def _is_main_check(self, test_node):
        return (
            isinstance(test_node, std_ast.Compare) and
            isinstance(test_node.left, std_ast.Name) and
            test_node.left.id == '__name__' and
            any(
                isinstance(op, std_ast.Eq) and
                isinstance(comparator, (std_ast.Constant, std_ast.Str)) and
                (
                    (hasattr(comparator, 'value') and comparator.value == '__main__') or
                    (hasattr(comparator, 's') and comparator.s == '__main__')
                )
                for op, comparator in zip(test_node.ops, test_node.comparators)
            )
        )

    def _get_call_name(self, node):
        """Get the call name and determine if it's an internal function call"""
        if isinstance(node, std_ast.Name):
            # Simple function call like 'foo()'
            # This is likely an internal function in the same module
            full_name = f"{self.current_module}.{node.id}"
            return full_name, True
            
        elif isinstance(node, std_ast.Attribute):
            # Attribute access like 'module.function()'
            base_obj = node.value
            
            # Handle direct project imports (e.g., from myproject import module)
            if isinstance(base_obj, std_ast.Name):
                # Simplified approach: assume one-part names are internal
                # packages have dots in them
                if '.' not in base_obj.id:
                    # Potentially an internal module, construct a qualified name
                    return f"{base_obj.id}.{node.attr}", True
            
            # Not an internal call
            return None, False
            
        return None, False

def extract_code_segments(file_path, function_name, call_positions):
    """Extract code segments between function calls"""
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Get function definition bounds
    with open(file_path, 'r', encoding='utf-8') as f:
        tree = std_ast.parse(f.read())
    
    function_node = None
    for node in std_ast.walk(tree):
        if isinstance(node, std_ast.FunctionDef) and node.name == function_name:
            function_node = node
            break
    
    if not function_node:
        return []
    
    # Get function body line range
    start_line = function_node.lineno
    end_line = 0
    for node in std_ast.walk(function_node):
        if hasattr(node, 'lineno'):
            end_line = max(end_line, getattr(node, 'end_lineno', node.lineno))
    
    # Sort call positions by line number
    call_positions.sort(key=lambda x: x['lineno'])
    
    # Extract code segments between calls
    segments = []
    last_pos = start_line
    
    for call in call_positions:
        if call['lineno'] > last_pos:
            # Extract code segment from last_pos to call_pos
            segment = ''.join(lines[last_pos:call['lineno']-1])
            if segment.strip():
                segments.append({
                    'type': 'code',
                    'content': segment.strip()
                })
        
        # Add the function call
        segments.append({
            'type': 'call',
            'content': call['source'],
            'callee': call['name'],
            'ast_id': call['ast_id']
        })
        
        last_pos = call['lineno']
    
    # Add final segment after last call
    if last_pos < end_line:
        segment = ''.join(lines[last_pos:end_line])
        if segment.strip():
            segments.append({
                'type': 'code',
                'content': segment.strip()
            })
    
    return segments

def build_call_tree(project_root, entry_file, entry_function, max_depth):
    # Convert paths to absolute
    project_root = os.path.abspath(project_root)
    entry_file = os.path.abspath(entry_file)
    
    # Build call graph and extract function code segments
    call_graph = defaultdict(list)
    function_segments = {}
    
    # Map of module names to file paths
    module_files = {}
    
    for py_file in Path(project_root).rglob('*.py'):
        # Get module name from file path
        relative_path = py_file.relative_to(project_root)
        if py_file.name == '__init__.py':
            module_name = '.'.join(relative_path.parent.parts)
        else:
            module_name = '.'.join(relative_path.with_suffix('').parts)
        
        module_files[module_name] = py_file
            
        with open(py_file, 'r', encoding='utf-8') as f:
            try:
                tree = std_ast.parse(f.read())
                
                builder = CallTreeBuilder(module_name, py_file)
                builder.visit(tree)
                
                # Update call graph with only internal calls
                for caller, calls in builder.call_graph.items():
                    if calls:  # Only add if there are calls
                        call_graph[caller] = calls
                
                # Create a map of function nodes and their code segments
                for func_name, calls in builder.call_graph.items():
                    # Skip __main__ blocks for now
                    if not func_name.endswith('__main__'):
                        # Extract the function name without module
                        simple_name = func_name.split('.')[-1]
                        function_segments[func_name] = extract_code_segments(
                            py_file, simple_name, calls
                        )
                
            except Exception as e:
                print(f"Error parsing {py_file}: {e}")
    
    # Handle __main__ entry point
    entry_point = None
    if entry_function == "__main__":
        entry_path = Path(entry_file).relative_to(project_root).with_suffix('')
        entry_point = f"{'.'.join(entry_path.parts)}.__main__"
    else:
        # Extract module name from entry file
        entry_path = Path(entry_file).relative_to(project_root)
        if entry_path.name == '__init__.py':
            module_name = '.'.join(entry_path.parent.parts)
        else:
            module_name = '.'.join(entry_path.with_suffix('').parts)
        
        entry_point = f"{module_name}.{entry_function}"
    
    # Check if entry point exists in call graph
    if entry_point not in call_graph:
        print(f"Warning: Entry point {entry_point} not found in call graph.")
        print("Available entry points:")
        for key in call_graph.keys():
            print(f"  - {key}")
        return None
    
    # Build the call tree
    def _recurse(current_func, depth, visited):
        if depth > max_depth or current_func in visited:
            return None
        
        # Split the function name to get just the function part
        module_parts = current_func.split('.')
        func_name = module_parts[-1]
        
        node = {
            "name": func_name,  # Just the function name
            "original": current_func,  # Full qualified name
            "children": []
        }
        
        # Add code segments if available
        if current_func in function_segments:
            node["segments"] = function_segments[current_func]
        
        if depth < max_depth:
            visited.add(current_func)
            
            # Add child function calls
            for call in call_graph.get(current_func, []):
                callee_name = call['name']
                
                # Create child node for the function call
                child_node = {
                    "name": callee_name.split('.')[-1],  # Just the function name
                    "original": callee_name,  # Full qualified name
                    "ast_id": call['ast_id'],
                    "call_line": call['source'],
                    "children": []
                }
                
                # Recursively process this child if it's in our call graph
                child_result = _recurse(callee_name, depth + 1, visited.copy())
                if child_result:
                    # Merge attributes from recursive result
                    for key, value in child_result.items():
                        if key != "name" and key != "original":  # Keep our name and original
                            child_node[key] = value
                
                node["children"].append(child_node)
                        
        return node
    
    # Start from entry point
    result = _recurse(entry_point, 1, set())
    return result

# # Complete runnable example
# if __name__ == "__main__":
#     import sys
    
#     if len(sys.argv) < 3:
#         print("Usage: python script.py project_root entry_file [entry_function] [max_depth]")
#         sys.exit(1)
    
#     project_root = sys.argv[1]
#     entry_file = sys.argv[2]
#     entry_function = sys.argv[3] if len(sys.argv) > 3 else "__main__"
#     max_depth = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    
#     call_tree = build_call_tree(
#         project_root=project_root,
#         entry_file=entry_file,
#         entry_function=entry_function,
#         max_depth=max_depth
#     )
    
#     print(json.dumps(call_tree, indent=2))