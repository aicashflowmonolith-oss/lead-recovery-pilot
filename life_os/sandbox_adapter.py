"""Candidate tests always require the OS sandbox; never fall back to host execution."""
import hashlib
import json
from pathlib import Path
import sys
from .ai_cli import command, run_bounded, CapabilityUnavailable, AdapterError


SAFE_BUILTINS = {"abs","all","any","bool","dict","enumerate","float","int","len","list",
    "max","min","range","round","set","sorted","str","sum","tuple","zip","ValueError","TypeError","Exception"}
MATH_ATTRIBUTES = {"isfinite","isnan","isinf","sqrt","floor","ceil","fabs","sin","cos","tan","log","log10","exp","pi","e"}


def validate_pure_python(source, callable_names=()):
    import ast, builtins
    tree=ast.parse(source)
    allowed={ast.Module,ast.FunctionDef,ast.arguments,ast.arg,ast.Return,ast.Expr,ast.Assert,
        ast.Assign,ast.AnnAssign,ast.AugAssign,ast.If,ast.For,ast.While,ast.Break,ast.Continue,
        ast.Pass,ast.Raise,ast.Try,ast.ExceptHandler,ast.Import,ast.alias,ast.Name,ast.Load,ast.Store,
        ast.Constant,ast.List,ast.Tuple,ast.Dict,ast.Set,ast.Subscript,ast.Slice,
        ast.BinOp,ast.UnaryOp,ast.BoolOp,ast.Compare,ast.IfExp,ast.Call,ast.keyword,ast.Attribute,
        ast.ListComp,ast.SetComp,ast.DictComp,ast.GeneratorExp,ast.comprehension,
        ast.Add,ast.Sub,ast.Mult,ast.Div,ast.FloorDiv,ast.Mod,ast.USub,ast.UAdd,ast.Not,
        ast.And,ast.Or,ast.Eq,ast.NotEq,ast.Lt,ast.LtE,ast.Gt,ast.GtE,ast.Is,ast.IsNot,ast.In,ast.NotIn}
    functions={n.name for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)} | set(callable_names)
    reserved=set(dir(builtins))-SAFE_BUILTINS
    for node in ast.walk(tree):
        if type(node) not in allowed:
            raise ValueError("Candidate requires unsupported Python syntax")
        name=getattr(node,'id',getattr(node,'arg',getattr(node,'name','')))
        if isinstance(name,str) and (name.startswith('__') or name in reserved):
            raise ValueError("Candidate requires an unavailable builtin")
        if isinstance(node,ast.FunctionDef) and node.decorator_list:
            raise ValueError("Candidate decorators are unavailable")
        if isinstance(node,ast.Import) and any(a.name!='math' or a.asname for a in node.names):
            raise ValueError("Only the math module is available to candidate tests")
        if isinstance(node,ast.Attribute) and not (isinstance(node.value,ast.Name) and node.value.id=='math' and node.attr in MATH_ATTRIBUTES and isinstance(node.ctx,ast.Load)):
            raise ValueError("Candidate attribute access is unavailable")
        if isinstance(node,ast.Call):
            valid=isinstance(node.func,ast.Name) and node.func.id in SAFE_BUILTINS|functions
            valid=valid or isinstance(node.func,ast.Attribute)
            if not valid: raise ValueError("Candidate dynamic calls are unavailable")
    return tree


def sandbox_command(directory, python_code):
    cli=command('codex')
    if not cli:
        raise CapabilityUnavailable('Windows sandbox requires the installed Codex CLI')
    # Windows requires root read for this sandbox. Pure-candidate validation and
    # restricted builtins prevent file/network calls; OS policy blocks writes.
    profile='permissions={lifeos_candidate={filesystem={":root"="read"},network={enabled=false}}}'
    return cli+['sandbox','-P','lifeos_candidate','-c',profile,'-C',str(directory),'--',
                sys.executable,'-I','-B','-c',python_code]


def probe(directory):
    cli=command('codex')
    if not cli: return False
    args=cli+['sandbox','-P','lifeos_probe','-c',
        'permissions={lifeos_probe={filesystem={":root"="read"},network={enabled=false}}}',
        '-C',str(directory),'--',sys.executable,'-I','-B','-c','print("LIFE_OS_SANDBOX_READY")']
    try:
        code,out,err=run_bounded(args,timeout=15)
    except (OSError,TimeoutError,AdapterError):
        return False
    return code==0 and out.strip()=='LIFE_OS_SANDBOX_READY'


def test_candidate(data,*,home,rid,pulse=None):
    if not isinstance(data,dict) or set(data)!={'source_step','tests'} or type(data['source_step']) is not int or not 0<=data['source_step']<32:
        raise ValueError('Tests must reference an earlier Python artifact step')
    if not isinstance(data['tests'],str) or not 1<=len(data['tests'])<=12000:
        raise ValueError('Bounded Python tests required')
    import ast
    tree=ast.parse(data['tests'])
    if not any(isinstance(n,ast.Assert) for n in ast.walk(tree)):
        raise ValueError('Candidate tests require assertions')
    directory=Path(home)/'execution/artifacts'/rid/str(data['source_step'])
    files=list(directory.glob('*.py'))
    if len(files)!=1:
        raise ValueError('Exactly one Python source artifact required')
    source=files[0]
    if not probe(directory):
        raise CapabilityUnavailable('Windows sandbox permission/setup repair required: restricted execution readiness check failed. Generated code was not run outside the sandbox.')
    if source.stat().st_size>64000:
        raise ValueError('Candidate source exceeds its bound')
    content=source.read_text(encoding='utf-8')
    source_tree=validate_pure_python(content)
    validate_pure_python(data['tests'], [n.name for n in ast.walk(source_tree) if isinstance(n,ast.FunctionDef)])
    # Restrict memory/CPU in the child before executing any candidate. A failure
    # to establish the resource limits aborts execution.
    limits = r"""
import ctypes,ctypes.wintypes as w
class BASIC(ctypes.Structure):
 _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
class IO(ctypes.Structure):
 _fields_=[('a',ctypes.c_ulonglong),('b',ctypes.c_ulonglong),('c',ctypes.c_ulonglong),('d',ctypes.c_ulonglong),('e',ctypes.c_ulonglong),('f',ctypes.c_ulonglong)]
class LIMITS(ctypes.Structure):
 _fields_=[('basic',BASIC),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.CreateJobObjectW.restype=w.HANDLE
k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
k.GetCurrentProcess.restype=w.HANDLE
job=k.CreateJobObjectW(None,None)
limits=LIMITS();limits.basic.flags=0x100|0x2;limits.basic.process_time=10*10_000_000;limits.process_memory=128*1024*1024
if not job or not k.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not k.AssignProcessToJobObject(job,k.GetCurrentProcess()):
 raise RuntimeError('Candidate resource isolation unavailable')
"""
    runner=limits+"\nimport builtins,math\n"
    runner+="safe={name:getattr(builtins,name) for name in "+repr(sorted(SAFE_BUILTINS))+"}\n"
    runner+="def import_math(name,*args,**kwargs):\n if name!='math': raise ImportError('unavailable')\n return math\n"
    runner+="safe['__import__']=import_math\nscope={'__builtins__':safe,'math':math}\n"
    runner+="exec(compile("+repr(content)+",'candidate','exec'),scope)\n"
    runner+="exec(compile("+repr(data['tests'])+",'candidate_tests','exec'),scope)\nprint('LIFE_OS_TESTS_PASSED')\n"
    code,out,err=run_bounded(sandbox_command(directory,runner),timeout=30,pulse=pulse)
    if code:
        if 'windows sandbox failed' in err or 'sandbox' in err.lower() and 'access' in err.lower():
            raise CapabilityUnavailable('Windows sandbox permission repair required: Codex .sandbox-bin access denied. Generated code was not run outside the sandbox.')
        raise AdapterError('Candidate tests failed in the restricted sandbox')
    if not out.rstrip().endswith('LIFE_OS_TESTS_PASSED'):
        raise AdapterError('Candidate test completion receipt missing')
    return 'Candidate assertions passed inside the restricted sandbox; production activation is separate.', {
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'tests_sha256':hashlib.sha256(data['tests'].encode()).hexdigest(),
        'verification':'sandboxed candidate assertions', 'independent_verification':False,
        'production_activated':False, 'memory_limit_mb':128, 'cpu_limit_seconds':10, 'candidate_policy':'pure-python-v1'}
