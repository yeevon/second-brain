class FakeFailingVaultWriter:
    def write_note(self, **kwargs):
        raise OSError("simulated vault write failure")
