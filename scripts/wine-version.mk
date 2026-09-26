dlls/ntdll/unix/version.c: dummy
	@version='const char wine_build[] = "wine-$(PACKAGE_VERSION)";' && \
	  (printf '%s\n' "$$version" | cmp -s - $@) || printf '%s\n' "$$version" >$@
