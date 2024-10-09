//@ts-check
import { decompress } from 'fflate';


/**
 * @typedef {Object} ZipFileEntry
 * @property {number} versionNeeded - The minimum version needed to extract the ZIP entry.
 * @property {number} bitFlag - The general purpose bit flag of the ZIP entry.
 * @property {number} compressionMethod - The compression method used for the ZIP entry.
 * @property {number} crc32 - The CRC-32 checksum of the uncompressed data.
 * @property {number} compressedSize - The size of the compressed data in bytes.
 * @property {number} uncompressedSize - The size of the uncompressed data in bytes.
 * @property {number} filenameLength - The length of the filename in bytes.
 * @property {number} extraFieldLength - The length of the extra field in bytes.
 * @property {Uint8Array} data - The compressed data for the ZIP entry.
 */


/**
 * @typedef {Object} CentralDirectoryEntry
 * @property {string} filename - The name of the file in the ZIP archive.
 * @property {number} compressionMethod - The compression method used for the file.
 * @property {number} compressedSize - The size of the compressed file in bytes.
 * @property {number} uncompressedSize - The size of the uncompressed file in bytes.
 * @property {number} fileOffset - The offset of the file's data in the ZIP archive.
 */

/**
 * Opens a remote ZIP file from the specified URL, fetches and parses the central directory, and provides a method to read files within the ZIP.
 *
 * @param {string} url - The URL of the remote ZIP file.
 * @returns {Promise<{ centralDirectory: Map<string, CentralDirectoryEntry>, readFile: function(string): Promise<Uint8Array> }>} A promise that resolves with an object containing:
 *  - `centralDirectory`: A map of filenames to their corresponding central directory entries.
 *  - `readFile`: A function to read a specific file from the ZIP archive by name.
 * @throws {Error} If the file is not found or an unsupported compression method is encountered.
 */
export const openRemoteZipFile = async (url) => {
    const response = await fetch(url, { method: 'HEAD' });
    const contentLength = Number(response.headers.get('Content-Length'));

    // Read the end of central directory record
    const eocdrBuffer = await fetchRange(url, contentLength - 22, contentLength - 1);
    const eocdrView = new DataView(eocdrBuffer.buffer);

    const centralDirOffset = eocdrView.getUint32(16, true);
    const centralDirSize = eocdrView.getUint32(12, true);

    // Fetch and parse the central directory
    const centralDirBuffer = await fetchRange(url, centralDirOffset, centralDirOffset + centralDirSize - 1);
    const centralDirectory = parseCentralDirectory(centralDirBuffer);
    return {
        centralDirectory: centralDirectory,
        readFile: async (file) => {
            const entry = centralDirectory.get(file);
            if (!entry) {
                throw new Error(`File not found: ${file}`);
            }

            // First, fetch the local file header (typically the first 30 bytes)
            const headerSize = 30; // Local file header is 30 bytes long by spec
            const headerData = await fetchRange(url, entry.fileOffset, entry.fileOffset + headerSize - 1);

            // Parse the local file header to get the filename length and extra field length
            const filenameLength = headerData[26] + (headerData[27] << 8); // 26-27 bytes in local header
            const extraFieldLength = headerData[28] + (headerData[29] << 8); // 28-29 bytes in local header
            const totalSizeToFetch = headerSize + filenameLength + extraFieldLength + entry.compressedSize;

            const fileData = await fetchRange(url, entry.fileOffset, entry.fileOffset + totalSizeToFetch - 1);

            const zipFileEntry = await parseZipFileEntry(file, fileData);
            if (zipFileEntry.compressionMethod === 0) {
                // No compression
                return zipFileEntry.data;
            } else if (zipFileEntry.compressionMethod === 8) {
                const results = await decompressAsync(zipFileEntry.data, { size: zipFileEntry.uncompressedSize });
                return results;
            } else {
                throw new Error(`Unsupported compressionMethod for file ${file}`);
            }
        }
    }
}

/**
* Fetches a range of bytes from a remote resource and returns it as a `Uint8Array`.
*
* @param {string} url - The URL of the remote resource to fetch.
* @param {number} start - The starting byte position of the range to fetch.
* @param {number} end - The ending byte position of the range to fetch.
* @returns {Promise<Uint8Array>} A promise that resolves to a `Uint8Array` containing the fetched byte range.
* @throws {Error} If there is an issue with the network request.
*/
const fetchRange = async (url, start, end) => {
    const response = await fetch(url, {
        headers: { Range: `bytes=${start}-${end}` }
    });
    const arrayBuffer = await response.arrayBuffer();
    return new Uint8Array(arrayBuffer);
}


/**
 * Asynchronously decompresses the provided data using the specified options.
 *
 * @param {Uint8Array} data - The compressed data to be decompressed.
 * @param {Object} opts - Options to configure the decompression process.
 * @returns {Promise<Uint8Array>} A promise that resolves with the decompressed data.
 * @throws {Error} If an error occurs during decompression, the promise is rejected with the error.
 */
const decompressAsync = async (data, opts) => {
    return new Promise((resolve, reject) => {
        decompress(data, opts, (err, result) => {
            if (err) {
                reject(err); // Reject the promise if there's an error
            } else {
                resolve(result); // Resolve the promise with the result
            }
        });
    });
}

/**
 * Extracts and parses the header and data of a compressed ZIP entry from raw binary data.
 *
 * @param {string} file - The name of the file stream to be parsed
 * @param {Uint8Array} rawData - The raw binary data containing the ZIP entry.
 * @returns {Promise<ZipFileEntry>} A promise that resolves to an object containing the ZIP entry's header information and compressed data.
 * @throws {Error} If the ZIP entry signature is invalid.
 */
const parseZipFileEntry = async (file, rawData) => {

    // Parse ZIP entry header
    const view = new DataView(rawData.buffer);
    let offset = 0;
    const signature = view.getUint32(offset, true);
    if (signature !== 0x04034b50) {
        throw new Error(`Invalid ZIP entry signature for ${file}`);
    }
    offset += 4;

    const versionNeeded = view.getUint16(offset, true);
    offset += 2;
    const bitFlag = view.getUint16(offset, true);
    offset += 2;
    const compressionMethod = view.getUint16(offset, true);
    offset += 2;
    offset += 4; // Skip last mod time and date
    const crc32 = view.getUint32(offset, true);
    offset += 4;
    const compressedSize = view.getUint32(offset, true);
    offset += 4;
    const uncompressedSize = view.getUint32(offset, true);
    offset += 4;
    const filenameLength = view.getUint16(offset, true);
    offset += 2;
    const extraFieldLength = view.getUint16(offset, true);
    offset += 2;

    offset += filenameLength + extraFieldLength;

    const data = rawData.subarray(offset, offset + compressedSize);
    return {
        versionNeeded,
        bitFlag,
        compressionMethod,
        crc32,
        compressedSize,
        uncompressedSize,
        filenameLength,
        extraFieldLength,
        data
    }
}

/**
 * Parses the central directory of a ZIP file from the provided buffer and returns a map of entries.
 *
 * @param {Uint8Array} buffer - The raw binary data containing the central directory of the ZIP archive.
 * @returns {Map<string, CentralDirectoryEntry>} A map where the key is the filename and the value is the corresponding central directory entry.
 * @throws {Error} If the buffer does not contain a valid central directory signature.
 */
const parseCentralDirectory = (buffer) => {
    let offset = 0;
    const view = new DataView(buffer.buffer);

    const entries = new Map();
    while (offset < buffer.length) {
        if (view.getUint32(offset, true) !== 0x02014b50) break; // Central directory signature

        const filenameLength = view.getUint16(offset + 28, true);
        const extraFieldLength = view.getUint16(offset + 30, true);
        const fileCommentLength = view.getUint16(offset + 32, true);

        const filename = new TextDecoder().decode(buffer.subarray(offset + 46, offset + 46 + filenameLength));

        const entry = {
            filename,
            compressionMethod: view.getUint16(offset + 10, true),
            compressedSize: view.getUint32(offset + 20, true),
            uncompressedSize: view.getUint32(offset + 24, true),
            fileOffset: view.getUint32(offset + 42, true),
        };

        entries.set(filename, entry);

        offset += 46 + filenameLength + extraFieldLength + fileCommentLength;
    }
    return entries;
}

