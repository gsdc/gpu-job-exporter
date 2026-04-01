Name:           gpu-job-exporter
Version:        1.0.0
Release:        1%{?dist}
Summary:        Prometheus exporter for GPU job completions (nvidia-smi + CPU time)
License:        MIT
BuildArch:      x86_64

# Source tarball is created by build_rpm.sh and includes vendored Python libs.
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  systemd-rpm-macros
Requires:       python3 >= 3.9
Requires:       nvidia-driver-cuda
# psutil & prometheus_client are vendored inside the tarball under lib/,
# so no extra RPM dependencies are needed at install time.

%description
Polls nvidia-smi every 2 seconds, detects GPU compute process completions,
measures per-job CPU time via /proc, and exposes Prometheus counters on
port 9101:
  - gpu_job_completed_total
  - gpu_job_cpu_time_seconds_total
  - gpu_job_duration_seconds (Summary)


# ── Prep ────────────────────────────────────────────────────────────────────
%prep
%setup -q


# ── Install ──────────────────────────────────────────────────────────────────
%install
# Main script + vendored Python libs
install -d %{buildroot}%{_libexecdir}/%{name}
cp -r lib            %{buildroot}%{_libexecdir}/%{name}/lib
install -m 0644 gpu_job_exporter.py \
                     %{buildroot}%{_libexecdir}/%{name}/gpu_job_exporter.py

# Wrapper script (sets PYTHONPATH so vendored libs are found)
install -d %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} << 'WRAPPER'
#!/bin/bash
export PYTHONPATH=%{_libexecdir}/%{name}/lib${PYTHONPATH:+:${PYTHONPATH}}
exec /usr/bin/python3 %{_libexecdir}/%{name}/gpu_job_exporter.py "$@"
WRAPPER
chmod 0755 %{buildroot}%{_bindir}/%{name}

# systemd unit
install -D -m 0644 %{name}.service \
    %{buildroot}%{_unitdir}/%{name}.service


# ── Scriptlets ───────────────────────────────────────────────────────────────
%pre
# Create dedicated system user/group on first install
getent group  gpu-exporter &>/dev/null || groupadd -r gpu-exporter
getent passwd gpu-exporter &>/dev/null || \
    useradd -r -g gpu-exporter -s /sbin/nologin \
            -c "GPU Job Exporter service account" gpu-exporter
exit 0

%post
%systemd_post %{name}.service

%preun
%systemd_preun %{name}.service

%postun
%systemd_postun_with_restart %{name}.service


# ── Files ────────────────────────────────────────────────────────────────────
%files
%{_bindir}/%{name}
%{_libexecdir}/%{name}/gpu_job_exporter.py
%{_libexecdir}/%{name}/lib/
%{_unitdir}/%{name}.service


# ── Changelog ────────────────────────────────────────────────────────────────
%changelog
* Wed Apr 01 2026 Geonmo Ryu <geonmo@kisti.re.kr> - 1.0.0-1
- Initial release
