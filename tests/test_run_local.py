"""Tests für run_local.py: Start bricht ab, wenn schon eine Instanz läuft.

Unter Windows startet ein zweites Gateway auf 0.0.0.0:8000 auch dann, wenn ein
altes auf 127.0.0.1:8000 lauscht; Host und Mitspieler landen dann bei
verschiedenen Gateways und das Spiel ist nicht mehr synchron.
"""

import socket

import run_local

# Ports weit weg von den Standardports, damit eine laufende Instanz nicht stört
BASES = ["--lobby-base", "47000", "--raft-base", "47100", "--control-port", "0"]


def listening_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    return server


def test_busy_ports_names_the_occupied_service():
    with listening_socket() as server:
        port = server.getsockname()[1]
        backend = run_local.Backend(run_local.parse_args(["--gateway-port", str(port), *BASES]))
        assert run_local.busy_ports(backend) == [(port, "Gateway")]
    backend = run_local.Backend(run_local.parse_args(["--gateway-port", str(port), *BASES]))
    assert run_local.busy_ports(backend) == []


def test_main_refuses_to_start_a_second_instance(capsys):
    with listening_socket() as server:
        port = server.getsockname()[1]
        assert run_local.main(["--gateway-port", str(port), "--no-console", *BASES]) == 1
    assert "Ports schon belegt" in capsys.readouterr().err
