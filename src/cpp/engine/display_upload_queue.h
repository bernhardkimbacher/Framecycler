#pragma once

#include "hw_frame_ticket.h"

#include <cstddef>
#include <cstdint>
#include <deque>
#include <vector>

enum class UploadQueuePolicy {
    EveryFrame = 0,
    Realtime = 1,
};

enum class UploadJobKind {
    CpuFloat16 = 0,
    HwImport = 1,
};

struct UploadJobRequest {
    int source_index = 0;
    int decoder_frame = 0;
    int upload_token = 0;
    UploadJobKind kind = UploadJobKind::CpuFloat16;
    int width = 0;
    int height = 0;
    int channels = 4;
};

struct UploadJob {
    int source_index = 0;
    int decoder_frame = 0;
    int width = 0;
    int height = 0;
    int channels = 0;
    int upload_token = 0;
    UploadJobKind kind = UploadJobKind::CpuFloat16;
    HwFrameTicket hw_ticket;
    enum class State {
        Queued = 0,
        Uploading = 1,
        Ready = 2,
        Failed = 3,
    };
    State state = State::Queued;
    void* texture = nullptr; // QRhiTexture*; owned until put/fail cleanup
    void* texture_uv = nullptr; // optional chroma plane (NV12 direct)
    int sample_mode = 0; // 0=RGBA, 1=NV12, 2=BGRA
    void* retained_cv_pixel_buffer = nullptr; // CFRetain'd; released by display cache
    size_t staging_slot = 0;
    uint64_t submit_generation = 0;
};

struct UploadQueueStats {
    int pending = 0;    // Queued
    int inflight = 0;   // Uploading
    int ready = 0;      // Ready awaiting put
    int completed = 0;  // lifetime puts handed off
    int refused = 0;    // EveryFrame full queue
    int coalesced = 0;  // Realtime replacements
};

// CPU-side queue policy + job bookkeeping. GPU work stays in RhiRenderer.
class DisplayUploadQueue {
public:
    static constexpr size_t kMaxPending = 64;

    void set_policy(UploadQueuePolicy policy);
    UploadQueuePolicy policy() const { return _policy; }

    // Returns true if a new Queued job was inserted.
    bool enqueue(const UploadJobRequest& req, bool already_resident);
    /// Enqueue a retained HW surface for GPU import (macOS VT → Metal).
    bool enqueue_hw(const UploadJobRequest& req, HwFrameTicket ticket, bool already_resident);

    bool has_job(int source_index, int decoder_frame) const;
    UploadJob* find_job(int source_index, int decoder_frame);

    // Up to max_k Queued jobs, marked Uploading with generation/staging assigned by caller after return.
    std::vector<UploadJob*> take_queued_for_submit(size_t max_k);

    void mark_uploading(UploadJob* job, uint64_t generation, size_t staging_slot);
    void mark_failed(UploadJob* job);
    // Remove a Queued/Failed job in-place (no texture ownership transfer).
    void discard(UploadJob* job);
    // Discard a Queued job for (source, frame) if present. Returns true if removed.
    bool discard_queued(int source_index, int decoder_frame);

    // Uploading jobs with submit_generation <= completed_generation become Ready.
    void complete_generation(uint64_t completed_generation);

    // Remove Ready jobs and return them (caller owns texture pointers).
    std::vector<UploadJob> take_ready();

    void clear();

    template <typename Fn>
    void clear_with(Fn&& destroy_texture)
    {
        for (auto& job : _jobs) {
            if (job.texture) {
                destroy_texture(job.texture);
                job.texture = nullptr;
            }
            if (job.texture_uv) {
                destroy_texture(job.texture_uv);
                job.texture_uv = nullptr;
            }
            // retained_cv_pixel_buffer ownership transfers on put_planar; if still
            // set here the renderer clear path must CFRelease it.
            job.retained_cv_pixel_buffer = nullptr;
        }
        clear();
    }

    UploadQueueStats stats() const;
    size_t job_count() const { return _jobs.size(); }

    // Remove Failed jobs (no textures).
    void compact_failed();

private:
    size_t count_state(UploadJob::State state) const;
    void erase_queued_for_source(int source_index);
    bool drop_oldest_queued();

    UploadQueuePolicy _policy = UploadQueuePolicy::EveryFrame;
    std::deque<UploadJob> _jobs;
    int _completed = 0;
    int _refused = 0;
    int _coalesced = 0;
};
