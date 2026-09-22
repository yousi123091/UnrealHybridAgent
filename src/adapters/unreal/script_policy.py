"""Operations that cannot safely execute inside the current UE tick RPC."""
import ast
from ...core.errors import NotSupported


def validate_editor_script(code: str) -> None:
    # UE 5.8 actually asserted in TickTaskManager during NewLevel from the MCP
    # game-thread callback. Block supported spellings across both live providers.
    # This is an API constraint, not a sandbox for hostile arbitrary Python.
    unsafe = {'new_level', 'new_level_from_template', 'load_level', 'open_level'}
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in unsafe:
            raise NotSupported('World replacement inside a live UE Python callback is disabled; open the target level at editor startup')
