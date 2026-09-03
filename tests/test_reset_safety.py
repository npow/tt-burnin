# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import os
import unittest
from unittest.mock import MagicMock, call, patch

from tt_burnin import utils


def fake_chip(interface_id, bdf, board_id=0x1234, architecture="blackhole"):
    chip = MagicMock()
    chip.get_pci_bdf.return_value = bdf
    chip.get_pci_interface_id.return_value = interface_id
    chip.board_id.return_value = board_id
    chip.as_bh.return_value = chip if architecture == "blackhole" else None
    chip.as_wh.return_value = chip if architecture == "wormhole" else None
    return chip


class ResetSafetyTests(unittest.TestCase):
    @patch("tt_burnin.utils.fcntl.ioctl")
    @patch("tt_burnin.utils.os.close")
    @patch("tt_burnin.utils.os.open", return_value=11)
    def test_reset_ioctl_always_uses_power_aware_open(
        self, open_device, _close_device, _ioctl
    ):
        self.assertTrue(utils._reset_device_ioctl(3, utils._RESET_FLAG_ASIC))
        flags = open_device.call_args.args[1]
        self.assertTrue(flags & os.O_APPEND)
        self.assertTrue(flags & os.O_CLOEXEC)

    @patch("tt_burnin.utils._reset_device_ioctl")
    @patch("tt_burnin.utils.os.close")
    @patch("tt_burnin.utils.os.open", return_value=11)
    @patch("tt_burnin.utils.os.path.exists", return_value=False)
    @patch("tt_burnin.utils.platform.machine", return_value="x86_64")
    @patch("tt_burnin.utils.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.utils.get_driver_version", return_value="2.11.0")
    @patch("tt_burnin.utils.PciChip")
    def test_every_target_is_preflighted_before_first_reset(
        self,
        pci_chip,
        _driver,
        _driver_check,
        _machine,
        _exists,
        _open_device,
        _close_device,
        reset_ioctl,
    ):
        pci_chip.side_effect = [
            fake_chip(0, "0000:41:00.0"),
            OSError("device is unavailable"),
        ]

        with self.assertRaisesRegex(RuntimeError, "interface 1"):
            utils.pci_board_reset([0, 1])

        reset_ioctl.assert_not_called()

    @patch("tt_burnin.utils.time.sleep")
    @patch("tt_burnin.utils.PciChip")
    @patch("tt_burnin.utils._wait_for_interface_at_bdf", return_value=7)
    @patch("tt_burnin.utils._wait_for_reset_completion")
    @patch("tt_burnin.utils._checked_reset_ioctl")
    @patch("tt_burnin.utils._preflight_reset_targets")
    def test_reset_uses_redetected_interface_for_post_reset(
        self,
        preflight,
        checked_ioctl,
        wait_for_completion,
        wait_for_interface,
        pci_chip,
        _sleep,
    ):
        target = utils._ResetTarget(
            interface_id=0,
            bdf="0000:41:00.0",
            board_id=0x1234,
            architecture="blackhole",
        )
        preflight.return_value = [target]
        redetected = fake_chip(7, target.bdf)
        pci_chip.return_value = redetected

        result = utils.pci_board_reset([0])

        self.assertEqual(result, [redetected])
        wait_for_completion.assert_called_once_with(target.bdf)
        wait_for_interface.assert_called_once_with(target.bdf)
        self.assertEqual(
            checked_ioctl.call_args_list,
            [
                call(0, utils._RESET_FLAG_PCIE_LINK, "PCIe link reset"),
                call(0, utils._RESET_FLAG_ASIC, "ASIC reset"),
                call(7, utils._RESET_FLAG_POST, "post-reset"),
            ],
        )

    @patch("tt_burnin.utils.time.sleep")
    @patch("tt_burnin.utils.PciChip")
    @patch("tt_burnin.utils._wait_for_interface_at_bdf", return_value=7)
    @patch("tt_burnin.utils._wait_for_reset_completion")
    @patch("tt_burnin.utils._checked_reset_ioctl")
    @patch("tt_burnin.utils._preflight_reset_targets")
    def test_reset_rejects_a_different_board_at_the_same_bdf(
        self,
        preflight,
        _checked_ioctl,
        _wait_for_completion,
        _wait_for_interface,
        pci_chip,
        _sleep,
    ):
        target = utils._ResetTarget(
            interface_id=0,
            bdf="0000:41:00.0",
            board_id=0x1234,
            architecture="blackhole",
        )
        preflight.return_value = [target]
        pci_chip.return_value = fake_chip(7, target.bdf, board_id=0x5678)

        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            utils.pci_board_reset([0])


if __name__ == "__main__":
    unittest.main()
