"""별도 프로세스에서 자원 한도를 설정한 뒤 bash로 교체한다.

진행 표시/병렬 도구 스레드가 있는 부모에서 preexec_fn을 실행하지 않는다.
"""
import os
import resource
import sys


def main():
    cpu, memory, file_size, processes = map(int, sys.argv[1:5])
    for kind, value in (
        (resource.RLIMIT_CPU, cpu), (resource.RLIMIT_AS, memory),
        (resource.RLIMIT_FSIZE, file_size), (resource.RLIMIT_NPROC, processes),
    ):
        if value <= 0:
            continue
        _, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(kind, (value, value))
    os.execv("/bin/bash", ["bash", "--noprofile", "--norc", "-c", sys.argv[5]])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"자원 한도/셸 시작 실패: {error}", file=sys.stderr)
        sys.exit(125)
