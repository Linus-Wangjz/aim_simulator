#include <filesystem>
#include <fstream>

#include <yaml-cpp/yaml.h>

#include "base/base.h"
#include "dram_controller/controller.h"
#include "dram_controller/plugin.h"

namespace Ramulator {

class DRAMTimingExporter : public IControllerPlugin, public Implementation {
    RAMULATOR_REGISTER_IMPLEMENTATION(
        IControllerPlugin,
        DRAMTimingExporter,
        "DRAMTimingExporter",
        "Exports resolved DRAM timing values to a YAML sidecar file.")

private:
    IDRAM *m_dram = nullptr;
    std::filesystem::path m_output_path;

public:
    void init() override {
        m_output_path = param<std::string>("path")
                            .desc("Path for the resolved DRAM timing YAML file")
                            .required();

        const auto parent_path = m_output_path.parent_path();
        if (!parent_path.empty()) {
            std::filesystem::create_directories(parent_path);
        }
    };

    void setup(IFrontEnd *frontend, IMemorySystem *memory_system) override {
        m_ctrl = cast_parent<IDRAMController>();
        m_dram = m_ctrl->m_dram;
    };

    void update(bool request_found, ReqBuffer::iterator &req_it) override {};

    void finalize() override {
        // The DRAM model is shared by every channel. Export once to avoid
        // duplicate writes from the per-channel controller-plugin instances.
        if (m_ctrl->m_channel_id != 0) {
            return;
        }

        YAML::Emitter emitter;
        emitter << YAML::BeginMap;
        emitter << YAML::Key << "impl" << YAML::Value << m_dram->m_impl->get_name();
        emitter << YAML::Key << "timing" << YAML::Value << YAML::BeginMap;
        for (int timing_id = 0; timing_id < m_dram->m_timings.size(); timing_id++) {
            emitter << YAML::Key << std::string(m_dram->m_timings(timing_id));
            emitter << YAML::Value << m_dram->m_timing_vals(timing_id);
        }
        emitter << YAML::EndMap;
        emitter << YAML::EndMap;

        std::ofstream output(m_output_path);
        if (!output) {
            throw ConfigurationError("Unable to write resolved DRAM timings to {}", m_output_path.string());
        }
        output << emitter.c_str() << '\n';
    }
};

} // namespace Ramulator
