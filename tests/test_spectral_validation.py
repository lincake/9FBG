"""Generated six-axis evidence only; no physical acceptance data."""
from copy import deepcopy
import time
import pytest

from ninefbg.validation import ValidationController
from ninefbg.spectral_validation import validated
from ninefbg.spectral_runtime import AXES, UNITS


def evidence(tolerances=None, end_frame=16):
    stamp=time.perf_counter_ns()-30_000_000
    context={"frame_id":end_frame,"optical_epoch":"optical","force_epoch":"force",
             "reference_id":"R1","zero_id":"Z1","model_checksum":"a"*64}
    truth=[.2,-.1,.3,.01,-.02,.03]
    prediction={"schema":"spectral_prediction_v1","valid":True,"comparison_ready":True,
                "status":"有效估计","point_id":"P01","wrench_pred":truth,
                "wrench_truth":truth,"wrench_error":[0.]*6,"axes":AXES,"units":UNITS}
    raw={"source_kind":"hardware","time_quality":"host_rx_approx","frame_id":3,
         "host_rx_monotonic_ns":stamp,"stream_epoch":"force","force_values":truth,"axes":AXES,"units":UNITS}
    return {"schema":"spectral_validation_evidence_v1","valid":True,"stale":False,"source_kind":"hardware",
            "experiment_running":False,"test_reference_ready":True,"truth_zero_ready":True,"reference_frozen":True,
            "age_ms":30.,"force_age_ms":30.,"host_rx_monotonic_ns":stamp,"model_id":"Sfixture",**context,
            "reference_version":None,"target_point_id":"P01","prediction_context":context,"truth_context":context,
            "prediction":prediction,"axes":AXES,"units":UNITS,"force_zero":[0.]*6,
            "sync":{"valid":True,"matched":True,"spectrum_frame_id":end_frame,"spectrum_stream_epoch":"optical",
                    "force_stream_epoch":"force","force_values":truth,"force_frame_ids":[3],"method":"exact_receive_time"},
            "force_source_frames":[raw],"spectral_reference":{"reference_id":"R1","stream_epoch":"optical",
                "S0":[10.]*512,"wavelength_nm":[1500.+i*.1 for i in range(512)],"effective_ns":stamp-1_000_000_000},
            "window":[{"frame_id":end_frame-15+i,"stream_epoch":"optical","intensity":[11.]*512,
                       "host_rx_monotonic_ns":stamp-(15-i)*40_000_000} for i in range(16)],
            "wrench_tolerances":tolerances or [None]*6}


def test_six_axis_tolerances_and_hash_chain_survive_reload(tmp_path):
    controller=ValidationController(tmp_path)
    controller.configure("a"*64,"Sfixture")
    sample=controller.capture(evidence())
    assert sample["force_tolerance_N"] is None
    with pytest.raises(ValueError,match="全部配置"):
        controller.judge(sample["sample_id"],True,True,"P01")
    controller.discard(sample["sample_id"])
    sample=controller.capture(evidence([.3]*3+[.005]*3, end_frame=32))
    controller.judge(sample["sample_id"],True,True,"P01")
    report=controller.finish()["report"]
    assert report["schema"]=="9fbg_spectral_manual_validation_report_v1"
    assert report["stats"]["wrench_mae"]==[0.]*6
    reloaded=ValidationController(tmp_path)
    assert reloaded.error is None
    reloaded.configure("a"*64,"Sfixture")
    assert reloaded.snapshot()["stats"]["judged_total"]==1
    assert reloaded.snapshot()["latest_report"]["report_sha256"]==report["report_sha256"]


def test_six_axis_source_boundaries_and_error_reconstruction_are_checked():
    original=evidence([.3]*6)
    assert validated(original)["prediction"]["wrench_truth"][1]<0
    altered=deepcopy(original)
    altered["window"][0]["stream_epoch"]="different"
    with pytest.raises(ValueError,match="窗口"):
        validated(altered)
    altered=deepcopy(original)
    altered["prediction"]["wrench_error"][3]=.1
    with pytest.raises(ValueError,match="误差"):
        validated(altered)
    altered=deepcopy(original)
    altered["sync"]["method"]="nearest_diagnostic_only"
    with pytest.raises(ValueError,match="诊断"):
        validated(altered)
